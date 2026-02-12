import logging
import random
import traceback
import uuid
from typing import Any, Dict, List, Optional

import ray
from ray._private import services
from ray._private.state import GlobalState
from ray._raylet import GcsClientOptions
from ray.util.state import list_nodes

from .....types import MODEL_KEY, NODE_ID, REPLICA_ID
from .evaluator import ModelEvaluator
from .node import CandidateLevel, Node, Resources
from .utils import gib, gib_map

logger = logging.getLogger("ndif")


class Cluster:
    def __init__(
        self,
        minimum_deployment_time_seconds: float = None,
        model_cache_percentage: float = 0.5,
    ):
        self.nodes: Dict[NODE_ID, Node] = {}

        self.evaluator = ModelEvaluator()

        self._state = None

        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage

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
                        available_gpus=list(range(int(total_gpus))),
                        gpu_memory_available_bytes_by_id={
                            gpu_id: int(gpu_memory_bytes)
                            for gpu_id in range(int(total_gpus))
                        },
                    ),
                    minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
                )

            logger.info(
                f"=> Node {name} updated with resources: {self.nodes[id].resources}"
            )

        for node_id in self.nodes.keys():
            if node_id not in current_nodes:
                node = self.nodes.pop(node_id)
                node.purge()

                logger.info(f"=> Node {node_id} removed from cluster")

    def _new_replica_id(self) -> REPLICA_ID:
        return uuid.uuid4().hex

    def target_replica_ids_for(
        self,
        cached_replica_ids: set[REPLICA_ID],
        deployed_replica_ids: set[REPLICA_ID],
        replicas: int,
    ) -> List[REPLICA_ID]:
        needed = max(0, replicas - len(deployed_replica_ids))

        # Start with cached IDs (up to needed)
        target_replica_ids = list(cached_replica_ids)[:needed]
        target_replica_id_set = set(target_replica_ids)

        # Generate new IDs for the remainder, avoiding deployed and cached IDs
        avoid = deployed_replica_ids | cached_replica_ids | target_replica_id_set
        while len(target_replica_ids) < needed:
            replica_id = self._new_replica_id()
            if replica_id in avoid:
                continue
            target_replica_ids.append(replica_id)
            avoid.add(replica_id)

        return target_replica_ids
    
    def deploy(self, model_keys: List[MODEL_KEY], dedicated: Optional[bool] = False, replicas: int = 1):
        """
        Deploy models on the cluster. This updates our internal state of the cluster.

        Args:
            model_keys (List[MODEL_KEY]): List of model keys to deploy
            dedicated (Optional[bool], optional): Whether to deploy the models as dedicated. Defaults to False.
            replicas (int, optional): Number of replicas to deploy. Defaults to 1.
        Returns:
            Dict[MODEL_KEY, str]: Dictionary of model keys and their deployment status
        """
        

        logger.info(
            f"Cluster deploying models: {model_keys}, dedicated: {dedicated}..."
        )

        results = {"result": {}, "evictions": set()}

        change = False

        deployed_replica_ids_by_model: dict[MODEL_KEY, set[REPLICA_ID]] = {
            model_key: set() for model_key in model_keys
        }
        cached_replica_ids_by_model: dict[MODEL_KEY, set[REPLICA_ID]] = {
            model_key: set() for model_key in model_keys
        }
        for node in self.nodes.values():
            for model_key in model_keys:
                for replica_id in node.deployments.get(model_key, {}):
                    deployed_replica_ids_by_model[model_key].add(replica_id)
                for replica_id in node.cache.get(model_key, {}):
                    cached_replica_ids_by_model[model_key].add(replica_id)

        target_replica_ids_by_model: dict[MODEL_KEY, List[REPLICA_ID]] = {
            model_key: self.target_replica_ids_for(
                cached_replica_ids_by_model[model_key],
                deployed_replica_ids_by_model[model_key],
                replicas,
            )
            for model_key in model_keys
        }

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

                for replica_id in target_replica_ids_by_model[model_key]:
                    results["result"][(model_key, replica_id)] = f"{size_in_bytes}\n{tb}"
                    
        # If this is a new dedicated set of models, we need to evict the dedicated deployments not found in the new set.
        if dedicated:
            logger.info("=> Checking to evict deprecated dedicated deployments...")

            for node in self.nodes.values():
                for model_key, model_map in list(node.deployments.items()):
                    for replica_id, deployment in list(model_map.items()):
                        if deployment.dedicated and model_key not in model_sizes_in_bytes:
                            logger.info(
                                f"==> Evicting deprecated dedicated deployment {model_key} from {node.name}"
                            )

                            results["evictions"].add((model_key, replica_id))

                            node.evict(model_key, replica_id, exclude=set(model_keys))

                            change = True

        # Sort models by size in descending order (deploy biggest ones first)
        sorted_models = sorted(
            model_sizes_in_bytes.items(), key=lambda x: x[1], reverse=True
        )

        # Record already-deployed replicas in results (they need no action but
        # downstream consumers must know about them for routing).
        for model_key in model_keys:
            for replica_id in deployed_replica_ids_by_model[model_key]:
                results["result"][(model_key, replica_id)] = CandidateLevel.DEPLOYED.name

        # For each model to deploy, find the best node to deploy it on, if possible.
        for model_key, size_in_bytes in sorted_models:
            size_gib = gib(size_in_bytes)
            logger.info(
                f"=> Analyzing deployment of {model_key} with size {size_in_bytes} ({size_gib:.2f} GiB)..."
            )
            for replica_id in target_replica_ids_by_model[model_key]:
                logger.info(
                    f"=> Analyzing deployment of {model_key} replica {replica_id} with size {size_in_bytes} ({size_gib:.2f} GiB)..."
                )

                candidates = {}

                # Check each node to see if the model can be deployed on it.
                for node in self.nodes.values():
                    logger.info(
                        f"==> Analyzing deployment of {model_key} with replica {replica_id} for node {node.name}..."
                    )

                    # Evaluate the node to see if the model can be deployed on it.
                    candidate = node.evaluate(model_key, replica_id, size_in_bytes, dedicated=dedicated)
                    gpu_mem_required_gib = gib(candidate.gpu_memory_required_bytes)

                    logger.info(
                        f"==> Candidate: {candidate.candidate_level.name}, gpus_required: {candidate.gpus_required}, "
                        f"gpu_mem_required_gib: {gpu_mem_required_gib}, evictions: {candidate.evictions}"
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

                if candidate_level == CandidateLevel.DEPLOYED:
                    logger.info(
                        f"=> {model_key} replica {replica_id} is already deployed on {self.nodes[node_id].name}"
                    )

                elif candidate_level == CandidateLevel.CANT_ACCOMMODATE:
                    logger.error(f"=> {model_key} replica {replica_id} cannot be deployed on any node")

                else:
                    gpu_mem_required_gib = gib(candidate.gpu_memory_required_bytes)
                    logger.info(
                        f"=> Deploying {model_key} replica {replica_id} with size {size_in_bytes} ({size_gib:.2f} GiB) "
                        f"on {self.nodes[node_id].name} because {candidate_level.name}. "
                        f"gpu_mem_required_gib: {gpu_mem_required_gib}, evictions: {candidate.evictions}"
                    )

                    self.nodes[node_id].deploy(
                        model_key,
                        replica_id,
                        candidate,
                        size_in_bytes,
                        dedicated=dedicated,
                        exclude=set(model_keys),
                    )
                    results["evictions"].update(candidate.evictions)
                    change = True
                    results["result"][(model_key, replica_id)] = candidate_level.name

                if (model_key, replica_id) not in results["result"]:
                    results["result"][(model_key, replica_id)] = candidate_level.name

        return results, change

    def evict(
        self,
        model_keys: List[MODEL_KEY],
        replica_keys: Optional[List[tuple[MODEL_KEY, REPLICA_ID]]] = None,
    ):
        """Evict models from the cluster.

        Returns:
            (results, change) tuple where:
            - results: dict mapping model_key to status
            - change: bool indicating if cluster state changed
        """
        change = False
        results = {}

        if replica_keys:
            for model_key, replica_id in replica_keys:
                found = False

                for node in self.nodes.values():
                    deployment = node.deployments.get(model_key, {}).get(replica_id)
                    if deployment is None:
                        continue
                    logger.info(f"gpu memory before evict: {node.resources.gpu_memory_available_bytes_by_id}")
                    node.evict(model_key, replica_id)
                    results[model_key, replica_id] = {
                        "status": "evicted",
                        "node": node.name,
                        "freed_gpus": len(deployment.gpus),
                        "freed_memory_gbs": deployment.size_bytes / 1024 / 1024 / 1024
                    }
                    logger.info(f"eviction results: {results}, gpu after evict: {node.resources.gpu_memory_available_bytes_by_id}")
                    change = True
                    found = True
                    break

                if not found:
                    results[model_key, replica_id] = {
                        "status": "not_found",
                    }
                
            return results, change

        for model_key in model_keys:

            # replica keys are not provided, we will evict all replicas for each model
            logger.info(f"evicting all replicas for model: {model_key}")
            found_any = False

            # search for deployments across all nodes
            for node in self.nodes.values():
                for deployment_model_key, model_map in list(node.deployments.items()):
                    if deployment_model_key != model_key:
                        continue
                    for deployment_replica_id, deployment in list(model_map.items()):
                        logger.info(
                            f"evicting replica: {deployment_model_key} {deployment_replica_id} for node: {node.name} gpu memory before evict: {node.resources.gpu_memory_available_bytes_by_id}"
                        )
                        node.evict(deployment_model_key, deployment_replica_id)
                        results[deployment_model_key, deployment_replica_id] = {
                            "status": "evicted",
                            "node": node.name,
                            "freed_gpus": len(deployment.gpus),
                            "freed_memory_gbs": deployment.size_bytes / 1024 / 1024 / 1024,
                        }
                        logger.info(
                            f"eviction results: {results}, gpu after evict: {node.resources.gpu_memory_available_bytes_by_id}"
                        )
                        change = True
                        found_any = True

            if not found_any:
                results[(model_key, None)] = {
                    "status": "not_found",
                }

        return results, change
