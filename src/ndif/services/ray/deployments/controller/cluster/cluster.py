import logging
import random
import traceback
from typing import Any, Dict, Optional

from ray._private import services
from ray._private.state import GlobalState
from ray._raylet import GcsClientOptions
from ray.util.state import list_nodes

from ......common.schema.controller import (
    DeploymentConfig,
    DeployResponse,
    ModelDeployResult,
    ReplicaState,
    ReplicaStates,
)
from ......common.types import MODEL_KEY, NODE_ID, REPLICA_ID
from .evaluator import ModelEvaluator
from .node import CandidateLevel, CPUResources, GPU, GPUResources, Node

logger = logging.getLogger("ndif.controller")


class Cluster:
    """In-memory model of the GPU cluster: its nodes, their resources, and the
    placement/eviction decisions over them."""

    def __init__(
        self,
        minimum_deployment_time_seconds: float = None,
        model_cache_percentage: float = 0.5,
        default_padding_factor: float = 0.15,
        default_padding_bias: int = 0,
        tp_model_actor_class: Optional[str] = None,
        default_model_actor_class: str = (
            "ndif.services.ray.deployments.modeling.base.ModelActor"
        ),
    ):
        self.nodes: Dict[NODE_ID, Node] = {}

        self.default_padding_factor = default_padding_factor
        self.default_padding_bias = default_padding_bias
        self.default_model_actor_class = default_model_actor_class
        self.evaluator = ModelEvaluator(
            padding_factor=default_padding_factor,
            padding_bias=default_padding_bias,
            tp_model_actor_class=tp_model_actor_class,
        )

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
        state = {
            "nodes": [node.get_state() for node in self.nodes.values()],
            "evaluator": self.evaluator.get_state(),
        }

        if include_ray_state:
            state["ray_state"] = self.state.cluster_resources()

        return state

    def update_nodes(self):
        """Sync the node set with Ray: add new GPU nodes, purge departed ones.

        Only GPU nodes are managed. Per-node GPU/CPU capacity is read from Ray's
        reported resources (cuda_memory_bytes / cpu_memory_bytes), with the CPU
        cache budget scaled by ``model_cache_percentage``.
        """
        logger.info("Updating nodes...")

        nodes = list_nodes(detail=True)
        current_nodes = set()

        for node in nodes:
            if "GPU" not in node.resources_total:
                continue

            id = node.node_id
            name = node.node_name

            current_nodes.add(id)

            if id not in self.nodes:
                total_gpus = int(node.resources_total["GPU"])
                gpu_type = node.labels.get("ray.io/accelerator-type", "unknown")
                per_gpu_memory_bytes = (
                    int(node.resources_total["cuda_memory_bytes"]) // total_gpus
                )
                cpu_memory_bytes = (
                    node.resources_total["cpu_memory_bytes"]
                    * self.model_cache_percentage
                )

                gpus = [
                    GPU(index=i, memory_bytes=per_gpu_memory_bytes)
                    for i in range(total_gpus)
                ]

                gpu_resources = GPUResources(gpu_type=gpu_type, gpus=gpus)

                cpu_resources = CPUResources(
                    memory_bytes=cpu_memory_bytes,
                    available_memory_bytes=cpu_memory_bytes,
                )

                self.nodes[id] = Node(
                    id,
                    name,
                    gpu_resources=gpu_resources,
                    cpu_resources=cpu_resources,
                    minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
                )

            logger.info(
                f"=> Node {name} updated with gpu_resources: "
                f"{self.nodes[id].gpu_resources}, cpu_resources: "
                f"{self.nodes[id].cpu_resources}"
            )

        for node_id in list(self.nodes.keys()):
            if node_id not in current_nodes:
                node = self.nodes.pop(node_id)
                node.purge()
                logger.info(f"=> Node {node_id} removed from cluster")

    def deploy(self, configs: Dict[MODEL_KEY, DeploymentConfig]) -> DeployResponse:
        """Place new replicas for each model on the best available node.

        ``config.replicas`` is additive. For each model, place that many new
        replicas, re-picking the best node each iteration (its evaluate()
        reflects state mutated by the previous placement). Biggest models go
        first; the first CANT_ACCOMMODATE ends that model's loop.

        Returns a DeployResponse: per-model results, the set of evicted
        (model_key, replica_id) pairs, and a change flag.
        """
        logger.info(
            f"Cluster deploying models: "
            f"{[(key, cfg.pinned, cfg.replicas) for key, cfg in configs.items()]}..."
        )

        response = DeployResponse()
        all_model_keys = set(configs.keys())

        # Evaluate sizes; record evaluator failures as per-model errors.
        evaluated_configs = []
        for model_key, config in configs.items():
            size_in_bytes = self.evaluator(
                model_key,
                padding_factor=config.padding_factor,
                dtype=config.dtype,
                trust_remote_code=config.trusted,
                size_bytes=config.size_bytes,
                padding_bias=config.padding_bias,
            )
            if isinstance(size_in_bytes, Exception):
                tb = "".join(
                    traceback.format_exception(
                        type(size_in_bytes),
                        size_in_bytes,
                        size_in_bytes.__traceback__,
                    )
                )
                logger.error(f"=> Model {model_key} failed to evaluate\n{tb}")
                model_result = response.results.setdefault(
                    model_key, ModelDeployResult()
                )
                model_result.error = f"{size_in_bytes}\n{tb}"
            else:
                evaluated_configs.append((model_key, config, size_in_bytes))

        # Deploy biggest models first.
        sorted_configs = sorted(evaluated_configs, key=lambda x: x[2], reverse=True)

        for model_key, config, size_in_bytes in sorted_configs:
            pinned = config.pinned
            model_result = response.results.setdefault(model_key, ModelDeployResult())

            for replica_index in range(config.replicas):
                logger.info(
                    f"=> Analyzing deployment of {model_key} "
                    f"(replica {replica_index + 1}/{config.replicas}) "
                    f"with size {size_in_bytes}..."
                )

                candidates: Dict[NODE_ID, Any] = {}
                best_level: Optional[CandidateLevel] = None

                # Asked once per replica rather than per node: it is a
                # property of the model, and on a cold entry it reaches the Hub.
                max_tp = self.evaluator.max_tp(
                    model_key,
                    dtype=config.dtype,
                    trust_remote_code=config.trusted,
                    override=config.max_tp,
                )

                for node in self.nodes.values():
                    candidate = node.evaluate(
                        model_key,
                        size_in_bytes,
                        pinned=pinned,
                        exclude=all_model_keys,
                        max_tp=max_tp,
                        gpus=config.gpus,
                    )

                    if best_level is None or candidate.candidate_level < best_level:
                        candidates = {node.id: candidate}
                        best_level = candidate.candidate_level
                    elif candidate.candidate_level == best_level:
                        candidates[node.id] = candidate
                    # Strictly worse than best_level -> ignore.

                if not candidates:
                    model_result.error = "No GPU nodes available."
                    break

                node_id, candidate = random.choice(list(candidates.items()))
                candidate_level = candidate.candidate_level

                if candidate_level == CandidateLevel.CANT_ACCOMMODATE:
                    logger.error(
                        f"=> {model_key} (replica {replica_index + 1}/"
                        f"{config.replicas}) cannot be deployed on any node — "
                        f"stopping further attempts for this model"
                    )
                    # A node that refused for a *nameable* reason says so; the
                    # rest really is "no room", and saying that when the model
                    # was simply asked for a count it cannot split into sends
                    # people to look at the cluster instead of their config.
                    stated = next(
                        (c.reason for c in candidates.values() if c.reason), None
                    )
                    model_result.error = stated or (
                        f"CANT_ACCOMMODATE: placed {len(model_result.replicas)} of "
                        f"{config.replicas} new replicas before the cluster ran out "
                        f"of room."
                    )
                    break

                logger.info(
                    f"=> Deploying {model_key} (replica {replica_index + 1}/"
                    f"{config.replicas}) on {self.nodes[node_id].name} because "
                    f"{candidate_level.name}. Requiring evictions: "
                    f"{candidate.evictions}"
                )

                new_replica_id = self.nodes[node_id].deploy(
                    model_key,
                    candidate,
                    size_in_bytes,
                    pinned=pinned,
                    exclude=all_model_keys,
                    execution_timeout_seconds=config.execution_timeout_seconds,
                    # An explicit actor_class wins; otherwise the evaluator
                    # picks, since whether this replica can run tensor-parallel
                    # is a property of the model and the cards it just got.
                    actor_class=config.actor_class
                    or self.evaluator.actor_class(
                        model_key,
                        len(candidate.gpus),
                        self.default_model_actor_class,
                        dtype=config.dtype,
                        trust_remote_code=config.trusted,
                        max_tp_override=config.max_tp,
                    ),
                    dtype=config.dtype,
                    trusted=config.trusted,
                )

                model_result.replicas.append(new_replica_id)
                response.evictions.update(candidate.evictions)
                response.change = True

            # Contract: every result has either replicas populated or error set.
            if not model_result.replicas and not model_result.error:
                model_result.error = "Could not accommodate any replicas at this time."

        return response

    def evict(
        self,
        model_key: MODEL_KEY,
        replica_id: Optional[REPLICA_ID] = None,
    ) -> ReplicaStates:
        """Evict replicas of a model.

        If ``replica_id`` is given, evict only that replica (HOT first, then
        WARM). Otherwise evict every HOT and WARM replica of ``model_key``.
        ``node.evict`` releases GPU memory and demotes HOT->WARM where CPU
        headroom allows; the returned snapshots reflect the pre-eviction state.
        """
        response = ReplicaStates()

        # Look up against the live dicts each time — node.evict's HOT->WARM
        # demotion can create a new inner cache dict, so cached refs go stale.
        for node in self.nodes.values():
            if replica_id is not None:
                dep = node.deployments.get(model_key, {}).get(replica_id) or node.cache.get(
                    model_key, {}
                ).get(replica_id)
                if dep is None:
                    continue
                response.replicas.append(ReplicaState(**dep.get_state()))
                node.evict(model_key, replica_id)
                break

            # Fan-out: every replica of this model on this node. The HOT->WARM
            # demotion preserves rid, so each eviction drains until both dicts
            # have no entry for it.
            rids = sorted(
                {
                    *node.deployments.get(model_key, {}).keys(),
                    *node.cache.get(model_key, {}).keys(),
                }
            )
            for rid in rids:
                dep = node.deployments.get(model_key, {}).get(rid) or node.cache.get(
                    model_key, {}
                ).get(rid)
                if dep is None:
                    continue
                response.replicas.append(ReplicaState(**dep.get_state()))
                while rid in node.deployments.get(model_key, {}) or rid in node.cache.get(
                    model_key, {}
                ):
                    node.evict(model_key, rid)

        return response
