import logging
import random
import traceback
from typing import Any, Dict, List

from ray._private import services
from ray._private.state import GlobalState
from ray._raylet import GcsClientOptions
from ray.util.state import list_nodes

from .....types import MODEL_KEY, NODE_ID
from .....schema import DeploymentConfig
from .evaluator import ResourceEvaluator
from .node import CandidateLevel, CPUResource, GPUResource, Node

logger = logging.getLogger("ndif")


class Cluster:
    def __init__(
        self,
        minimum_deployment_time_seconds: float | None = None,
        model_cache_percentage: float = 0.5,
        skip_head_node_for_deployment: bool = True,
    ):
        self.nodes: Dict[NODE_ID, Node] = {}

        self.resource_evaluator = ResourceEvaluator()

        self._state = None

        self.minimum_deployment_time_seconds: float | None = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage
        self.skip_head_node_for_deployment = skip_head_node_for_deployment

    @property
    def state(self):
        if self._state is None:
            address = services.canonicalize_bootstrap_address_or_die(None)

            state = GlobalState()
            options = GcsClientOptions.create(
                address, None, allow_cluster_id_nil=True, fetch_cluster_id_if_nil=False
            )
            state._initialize_global_state(options)

            self._state = state

        return self._state

    def get_state(self, include_ray_state: bool = False) -> Dict[str, Any]:
        """Get the state of the cluster."""

        state = {
            "nodes": [node.get_state() for node in self.nodes.values()],
            "resource_evaluator": self.resource_evaluator.get_state(),
        }

        if include_ray_state:
            # TODO: The choice of cluster_resources() was arbitrary, GlobalState exposes a lot of potentially useful ray cluster information
            state["ray_state"] = self.state.cluster_resources()

        return state

    def update_nodes(self):
        logger.info("Updating nodes...")

        nodes = list_nodes(detail=True)
        current_nodes = set()

        for node in nodes:

            id = node.node_id
            name = node.node_name

            current_nodes.add(id)

            if id not in self.nodes:
                # Get gpu resources
                total_gpus = 0
                gpu_memory_bytes = 0
                if "GPU" in node.resources_total:
                    # Real GPU node
                    total_gpus = node.resources_total["GPU"]
                    gpu_memory_bytes = node.resources_total["cuda_memory_bytes"] / total_gpus

                # Get cpu resources
                cpu_memory_bytes = (
                    node.resources_total["cpu_memory_bytes"]
                    * self.model_cache_percentage
                )

                is_head_node = node.resources_total.get("head", 0) > 0

                self.nodes[id] = Node(
                    id,
                    name,
                    cpu_resource=CPUResource(
                        cpu_memory_bytes=cpu_memory_bytes,
                        available_cpu_memory_bytes=cpu_memory_bytes,
                    ),
                    gpu_resource=GPUResource(
                        total_gpus=total_gpus,
                        gpu_memory_bytes=gpu_memory_bytes,
                        available_gpus=list(range(int(total_gpus))),
                    ),
                    minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
                    is_head_node=is_head_node,
                )

            logger.info(
                f"=> Node {name} updated with resources: cpu={self.nodes[id].cpu_resource}, gpu={self.nodes[id].gpu_resource}"
            )

        for node_id in self.nodes.keys():
            if node_id not in current_nodes:
                node = self.nodes.pop(node_id)
                node.purge()

                logger.info(f"=> Node {node_id} removed from cluster")

    def deploy(self, models: List[DeploymentConfig]):
        """
        Deploy models on the cluster. This updates our internal state of the cluster.

        Args:
            models (List[DeploymentConfig]): List of deployment configurations to deploy

        Returns:
            Dict[MODEL_KEY, str]: Dictionary of model keys and their deployment status
        """

        logger.info(
            "Cluster deploying models \n" + "\n".join(
                [
                    f"  - {deployment_cfg.model_key}: {deployment_cfg}"
                    for deployment_cfg in models
                ]
            )
        )

        results = {"result": {}, "evictions": set()}

        change = False

        # Get the size of the models in bytes
        model_sizes_in_bytes: Dict[MODEL_KEY, int] = {}
        for deployment_cfg in models:
            model_key = deployment_cfg.model_key
            padding_factor = deployment_cfg.padding_factor
            size_in_bytes = self.resource_evaluator(model_key, padding_factor=padding_factor)
            if isinstance(size_in_bytes, Exception):
                tb = "".join(
                    traceback.format_exception(
                        type(size_in_bytes), size_in_bytes, size_in_bytes.__traceback__
                    )
                )
                logger.error(f"=> Model {model_key} failed to evaluate\n{tb}")

                results["result"][model_key] = f"{size_in_bytes}\n{tb}"

            else:
                model_sizes_in_bytes[model_key] = size_in_bytes


        dedicated_model_keys: set[MODEL_KEY] = {
            deployment_cfg.model_key for deployment_cfg in models if deployment_cfg.dedicated
        }
        # If there are dedicated models to deploy, evict currently dedicated models that aren't in this set.
        if len(dedicated_model_keys) > 0:
            logger.info("=> Checking to evict deprecated dedicated deployments...")

            for node in self.nodes.values():
                for model_key, deployment in list(node.deployments.items()):
                    if deployment.dedicated and model_key not in dedicated_model_keys:
                        logger.info(
                            f"==> Evicting deprecated dedicated deployment {model_key} from {node.name}"
                        )

                        results["evictions"].add(model_key)

                        node.evict(model_key, exclude=dedicated_model_keys)

                        change = True

        # Filter to only models that passed evaluation, then sort by size descending, dedicated first
        evaluated_models = [m for m in models if m.model_key in model_sizes_in_bytes]
        sorted_models = sorted(
            evaluated_models, key=lambda x: (x.dedicated, model_sizes_in_bytes[x.model_key]), reverse=True
        )

        # For each model to deploy, find the best node to deploy it on, if possible.
        for deployment_cfg in sorted_models:
            model_key = deployment_cfg.model_key
            size_in_bytes = model_sizes_in_bytes[model_key]
            logger.info(
                f"=> Analyzing deployment of {deployment_cfg.model_key} with size {size_in_bytes}..."
            )

            candidates = {}

            # Check each node to see if the model can be deployed on it.
            for node in self.nodes.values():
                # Skip head node if configured to do so
                if self.skip_head_node_for_deployment and node.is_head_node:
                    logger.info(
                        f"==> Skipping head node {node.name} for deployment"
                    )
                    continue

                logger.info(
                    f"==> Analyzing deployment of {model_key} for node {node.name}..."
                )

                # Evaluate the node to see if the model can be deployed on it.
                candidate = node.evaluate(
                    model_key, size_in_bytes, deployment_cfg=deployment_cfg
                )

                logger.info(
                    f"==> Candidate: {candidate.candidate_level.name}, gpus_required: {candidate.gpus_required}, evictions: {candidate.evictions}"
                )

                # If the model is already deployed on this node, we can stop looking for nodes.
                if candidate.candidate_level == CandidateLevel.DEPLOYED:
                    candidates = {node.id: candidate}

                    break

                # If we haven't found a node yet, add this one to the candidates.
                if len(candidates) == 0:
                    candidates[node.id] = candidate

                # If we have found a node, we need to see if this node is better than the current best node.
                else:
                    candidate_level = list(candidates.values())[0].candidate_level

                    # If the candidate is the same level as the current best node, we can add it to the candidates.
                    if candidate.candidate_level == candidate_level:
                        candidates[node.id] = candidate

                    # If the candidate is better than the current best node, we can replace the current candidate set with just this one.
                    elif candidate.candidate_level < candidate_level:
                        candidates = {node.id: candidate}

            # Pick a random node from the candidates.
            if len(candidates) == 0:
                results["result"][model_key] = "CANT_ACCOMMODATE"
                logger.error(f"=> {model_key} cannot be deployed on any node")
            else:
                node_id, candidate = random.choice(list(candidates.items()))

                candidate_level = candidate.candidate_level

                results["result"][model_key] = candidate_level.name

                if candidate_level == CandidateLevel.DEPLOYED:
                    logger.info(
                        f"=> {model_key} is already deployed on {self.nodes[node_id].name}"
                    )

                elif candidate_level == CandidateLevel.CANT_ACCOMMODATE:
                    logger.error(f"=> {model_key} cannot be deployed on any node")

                else:
                    logger.info(
                        f"=> Deploying {model_key} with size {size_in_bytes} on {self.nodes[node_id].name} because {candidate_level.name}. Requiring evictions: {candidate.evictions}"
                    )

                    self.nodes[node_id].deploy(
                        model_key,
                        candidate,
                        size_in_bytes,
                        deployment_cfg=deployment_cfg,
                        exclude=dedicated_model_keys,
                    )

                    results["evictions"].update(candidate.evictions)

                    change = True

        return results, change

    def evict(self, model_keys: List[MODEL_KEY]):
        """Evict models from the cluster.

        Returns:
            (results, change) tuple where:
            - results: dict mapping model_key to status
            - change: bool indicating if cluster state changed
        """
        change = False
        results: Dict[MODEL_KEY, Dict[str, Any]] = {}

        for model_key in model_keys:
            found = False

            # Search for deployment across all nodes
            for node_id, node in self.nodes.items():
                if model_key in node.deployments:
                    deployment = node.deployments[model_key]

                    # Evict from node (updates resources, removes from deployments)
                    node.evict(model_key)

                    results[model_key] = {
                        "status": "evicted",
                        "node": node.name,
                        "freed_gpus": len(deployment.gpus),
                        "freed_memory_gbs": deployment.size_bytes / 1024 / 1024 / 1024
                    }
                    change = True
                    found = True
                    break

            if not found:
                results[model_key] = {"status": "not_found"}

        return results, change