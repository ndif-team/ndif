import logging
import random
import traceback
from typing import Any, Dict, List, Optional

import ray
from ray._private import services
from ray._private.state import GlobalState
from ray._raylet import GcsClientOptions
from ray.util.state import list_nodes

from ndif_common.types import MODEL_KEY
from .evaluator import ModelEvaluator
from .node import CandidateLevel, Node, Resources

logger = logging.getLogger("ndif")


class Cluster:
    def __init__(
        self,
        minimum_deployment_time_seconds: float = None,
        model_cache_percentage: float = 0.5,
    ):
        self.nodes: Dict[str, Node] = {}

        self.evaluator = ModelEvaluator()

        self._state = None

        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage

        self.update_nodes()

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
            "evaluator": self.evaluator.get_state(),
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
            if "GPU" not in node.resources_total:
                # We currently only do resource management for nodes with GPUs
                continue

            id = node.node_id
            name = node.node_name

            current_nodes.add(id)

            if id not in self.nodes:
                total_gpus = node.resources_total["GPU"]
                gpu_type = "TEST"
                # gpu_type = node.resources_total["GPU_TYPE"]
                gpu_memory_bytes = (
                    (node.resources_total["cuda_memory_bytes"]) / total_gpus
                )
                cpu_memory_bytes = (
                    node.resources_total["cpu_memory_bytes"]
                    * self.model_cache_percentage
                )

                self.nodes[id] = Node(
                    id,
                    name,
                    Resources(
                        total_gpus=total_gpus,
                        gpu_type=gpu_type,
                        gpu_memory_bytes=gpu_memory_bytes,
                        cpu_memory_bytes=cpu_memory_bytes,
                        available_cpu_memory_bytes=cpu_memory_bytes,
                        available_gpus=total_gpus,
                    ),
                    minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
                )

            logger.info(
                f"=> Node {name} updated with resources: {self.nodes[id].resources}"
            )

        for node in self.nodes.keys():
            if node not in current_nodes:
                del self.nodes[node]

                logger.info(f"=> Node {node} removed from cluster")

    def deploy(self, model_keys: List[MODEL_KEY], dedicated: Optional[bool] = False):
        """
        Deploy models on the cluster. This updates our internal state of the cluster.

        Args:
            model_keys (List[MODEL_KEY]): List of model keys to deploy
            dedicated (Optional[bool], optional): Whether to deploy the models as dedicated. Defaults to False.

        Returns:
            Dict[MODEL_KEY, str]: Dictionary of model keys and their deployment status
        """

        logger.info(
            f"Cluster deploying models: {model_keys}, dedicated: {dedicated}..."
        )

        results = {"result": {}, "evictions": set()}

        change = False

        # First get the size of the models in bytes
        model_sizes_in_bytes = {
            model_key: self.evaluator(model_key) for model_key in model_keys
        }

        for model_key, size_in_bytes in list(model_sizes_in_bytes.items()):
            if isinstance(size_in_bytes, Exception):
                tb = "".join(
                    traceback.format_exception(
                        type(size_in_bytes), size_in_bytes, size_in_bytes.__traceback__
                    )
                )
                logger.error(f"=> Model {model_key} failed to evaluate\n{tb}")

                del model_sizes_in_bytes[model_key]

                results["result"][model_key] = f"{size_in_bytes}\n{tb}"

        # If this is a new dedicated set of models, we need to evict the dedicated deployments not found in the new set.
        if dedicated:
            logger.info("=> Checking to evict deprecated dedicated deployments...")

            cache_futures = []

            for node in self.nodes.values():
                for model_key, deployment in list(node.deployments.items()):
                    if deployment.dedicated and model_key not in model_sizes_in_bytes:
                        logger.info(
                            f"==> Evicting deprecated dedicated deployment {model_key} from {node.name}"
                        )

                        results["evictions"].add(model_key)

                        if node.evict(model_key, exclude=set(model_keys)):
                            cache_future = deployment.cache()

                            if cache_future is not None:
                                cache_futures.append(cache_future)

                        change = True

            ray.get(cache_futures)

        # Sort models by size in descending order (deploy biggest ones first)
        sorted_models = sorted(
            model_sizes_in_bytes.items(), key=lambda x: x[1], reverse=True
        )

        # For each model to deploy, find the best node to deploy it on, if possible.
        for model_key, size_in_bytes in sorted_models:
            logger.info(
                f"=> Analyzing deployment of {model_key} with size {size_in_bytes}..."
            )

            candidates = {}

            # Check each node to see if the model can be deployed on it.
            for node in self.nodes.values():
                logger.info(
                    f"==> Analyzing deployment of {model_key} for node {node.name}..."
                )

                # Evaluate the node to see if the model can be deployed on it.
                candidate = node.evaluate(model_key, size_in_bytes, dedicated=dedicated)

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
                    dedicated=dedicated,
                    exclude=set(model_keys),
                )

                results["evictions"].update(candidate.evictions)

                change = True

        return results, change
