import logging
import random
import traceback
from typing import Any, Dict, Optional

from ray._private import services
from ray._private.state import GlobalState
from ray._raylet import GcsClientOptions
from ray.util.state import list_nodes

from ......common.schema.controller import (
    DeployResponse,
    ModelDeployResult,
    ReplicaState,
    ReplicaStates,
)
from ......common.schema.deployment_config import DeploymentConfig
from ......common.tracing import trace_span
from ......common.types import MODEL_KEY, NODE_ID, REPLICA_ID
from .evaluator import ModelEvaluator
from .node import CandidateLevel, CPUResources, GPU, GPUResources, Node

logger = logging.getLogger("ndif")


class Cluster:
    def __init__(
        self,
        minimum_deployment_time_seconds: float = None,
        model_cache_percentage: float = 0.5,
        default_padding_factor: float = 0.15,
        default_padding_bias: int = 0,
        default_model_actor_class: str = "ndif.services.ray.deployments.modeling.base.ModelActor",
    ):
        self.nodes: Dict[NODE_ID, Node] = {}

        self.default_padding_factor = default_padding_factor
        self.default_padding_bias = default_padding_bias
        self.default_model_actor_class = default_model_actor_class
        self.evaluator = ModelEvaluator(padding_factor=default_padding_factor, padding_bias=default_padding_bias)

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
                total_gpus = int(node.resources_total["GPU"])
                gpu_type = node.labels.get("ray.io/accelerator-type", "unknown")
                per_gpu_memory_bytes = int(
                    node.resources_total["cuda_memory_bytes"]
                ) // total_gpus
                cpu_memory_bytes = (
                    node.resources_total["cpu_memory_bytes"]
                    * self.model_cache_percentage
                )

                gpus = [
                    GPU(index=i, memory_bytes=per_gpu_memory_bytes)
                    for i in range(total_gpus)
                ]

                gpu_resources = GPUResources(
                    gpu_type=gpu_type,
                    gpus=gpus,
                )

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
                f"=> Node {name} updated with gpu_resources: {self.nodes[id].gpu_resources}, cpu_resources: {self.nodes[id].cpu_resources}"
            )

        for node_id in self.nodes.keys():
            if node_id not in current_nodes:
                node = self.nodes.pop(node_id)
                node.purge()

                logger.info(f"=> Node {node_id} removed from cluster")

    def deploy(self, configs: Dict[MODEL_KEY, DeploymentConfig]) -> DeployResponse:
        """
        Deploy models on the cluster. This updates our internal state of the cluster.

        ``config.replicas`` is **additive**: every call places that many new
        replicas regardless of what's already running. To shrink or replace
        existing replicas, use ``Cluster.evict`` (model-wide or single-replica
        targeted via the ``replica_id`` arg).

        Args:
            configs: Dict mapping model keys to their DeploymentConfig.

        Returns:
            A DeployResponse: per-model results keyed by model_key (new/old
            replica ids and an optional error string), the set of evicted
            (model_key, replica_id) pairs, and a change flag.
        """

        with trace_span("cluster.deploy", attributes={
            "ndif.cluster.num_models": len(configs),
            "ndif.cluster.num_nodes": len(self.nodes),
        }) as span:
            logger.info(
                f"Cluster deploying models: "
                f"{[(key, cfg.pinned, cfg.replicas) for key, cfg in configs.items()]}..."
            )

            response = DeployResponse()
            all_model_keys = set(configs.keys())

            # Evaluate sizes; record evaluator failures as per-model errors.
            evaluated_configs = []
            for model_key, config in configs.items():
                size_in_bytes = self.evaluator(model_key, padding_factor=config.padding_factor)
                if isinstance(size_in_bytes, Exception):
                    tb = "".join(
                        traceback.format_exception(
                            type(size_in_bytes), size_in_bytes, size_in_bytes.__traceback__
                        )
                    )
                    logger.error(f"=> Model {model_key} failed to evaluate\n{tb}")
                    span.add_event("model_evaluation_failed", {"model_key": model_key})
                    model_result = response.results.setdefault(
                        model_key, ModelDeployResult()
                    )
                    model_result.error = f"{size_in_bytes}\n{tb}"
                else:
                    evaluated_configs.append((model_key, config, size_in_bytes))

            # Deploy biggest models first.
            sorted_configs = sorted(evaluated_configs, key=lambda x: x[2], reverse=True)

            # For each model, place ``config.replicas`` new replicas, picking the
            # best node fresh each iteration (evaluate() reflects state mutated
            # by the previous replica's placement). The first CANT_ACCOMMODATE
            # ends the loop for that model — the cluster only loses headroom
            # from here, so subsequent attempts would fail the same way.
            for model_key, config, size_in_bytes in sorted_configs:
                pinned = config.pinned
                model_result = response.results.setdefault(
                    model_key, ModelDeployResult()
                )

                for replica_index in range(config.replicas):
                    logger.info(
                        f"=> Analyzing deployment of {model_key} "
                        f"(replica {replica_index + 1}/{config.replicas}) "
                        f"with size {size_in_bytes}..."
                    )

                    candidates: Dict[NODE_ID, Any] = {}
                    best_level: Optional[CandidateLevel] = None

                    for node in self.nodes.values():
                        logger.info(
                            f"==> Analyzing deployment of {model_key} for node {node.name}..."
                        )

                        candidate = node.evaluate(
                            model_key,
                            size_in_bytes,
                            pinned=pinned,
                            exclude=all_model_keys,
                        )

                        logger.info(
                            f"==> Candidate: {candidate.candidate_level.name}, "
                            f"gpus: {candidate.gpus}, evictions: {candidate.evictions}"
                        )

                        if best_level is None or candidate.candidate_level < best_level:
                            candidates = {node.id: candidate}
                            best_level = candidate.candidate_level
                        elif candidate.candidate_level == best_level:
                            candidates[node.id] = candidate
                        # Strictly worse than best_level → ignore.

                    node_id, candidate = random.choice(list(candidates.items()))
                    candidate_level = candidate.candidate_level

                    if candidate_level == CandidateLevel.CANT_ACCOMMODATE:
                        logger.error(
                            f"=> {model_key} (replica {replica_index + 1}/{config.replicas}) "
                            f"cannot be deployed on any node — stopping further "
                            f"attempts for this model"
                        )
                        model_result.error = (
                            f"CANT_ACCOMMODATE: placed "
                            f"{len(model_result.replicas)} of {config.replicas} "
                            f"new replicas before the cluster ran out of room."
                        )
                        span.add_event(
                            "model_placement_failed",
                            {
                                "model_key": model_key,
                                "replicas_placed": len(model_result.replicas),
                                "replicas_requested": config.replicas,
                            },
                        )
                        break

                    logger.info(
                        f"=> Deploying {model_key} (replica "
                        f"{replica_index + 1}/{config.replicas}) with size "
                        f"{size_in_bytes} on {self.nodes[node_id].name} because "
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
                        actor_class=config.actor_class or self.default_model_actor_class,
                    )

                    model_result.replicas.append(new_replica_id)
                    response.evictions.update(candidate.evictions)
                    response.change = True

                    span.add_event("model_placement_decided", {
                        "model_key": model_key,
                        "replica_index": replica_index,
                        "candidate_level": candidate_level.name,
                        "node": self.nodes[node_id].name if node_id in self.nodes else "unknown",
                        "size_bytes": size_in_bytes,
                    })

                # Contract: every result we return has either replicas
                # populated or error set, so callers don't have to interpret
                # an empty-and-silent result (e.g. config.replicas was 0 to
                # begin with, or some edge case the loop didn't touch).
                if not model_result.replicas and not model_result.error:
                    model_result.error = (
                        "Could not accommodate any replicas at this time."
                    )

            span.set_attribute(
                "ndif.cluster.total_evictions", len(response.evictions)
            )

            return response

    def evict(
        self,
        model_key: MODEL_KEY,
        replica_id: Optional[REPLICA_ID] = None,
    ) -> ReplicaStates:
        """Evict replicas of a model.

        If ``replica_id`` is given, evict only that specific replica
        (matched first in HOT, then in WARM). Otherwise evict every HOT and
        WARM replica of ``model_key``.

        ``node.evict()`` releases GPU memory and, where CPU headroom allows,
        demotes HOT → WARM; only when no cache room can be made is the
        replica fully removed. The returned ReplicaState snapshots are taken
        before that transition, so they always reflect the replica's pre-
        eviction state.

        Returns:
            ReplicaStates listing the replicas that were evicted (empty if
            nothing matched the query).
        """
        with trace_span(
            "cluster.evict",
            attributes={
                "ndif.model.key": model_key,
                "ndif.replica.id": replica_id or "",
            },
        ) as span:
            response = ReplicaStates()

            # Lookups against node.deployments / node.cache always go inline
            # against the live dict — caching a local ref is unsafe because
            # node.evict's HOT->WARM demotion creates a new inner dict via
            # ``setdefault`` when the model_key wasn't already present in
            # cache, so any pre-captured ref would go stale.
            for node in self.nodes.values():
                if replica_id is not None:
                    # Targeted: single node.evict. HOT may demote to WARM
                    # (preserving rid) — that's intentional, the user can
                    # promote it back via the dot's Deploy. WARM evicts
                    # fully. Rids are unique cluster-wide so we stop after
                    # the first hit.
                    dep = (
                        node.deployments.get(model_key, {}).get(replica_id)
                        or node.cache.get(model_key, {}).get(replica_id)
                    )
                    if dep is None:
                        continue
                    response.replicas.append(ReplicaState(**dep.get_state()))
                    node.evict(model_key, replica_id)
                    span.add_event(
                        "replica_evicted",
                        {
                            "model_key": model_key,
                            "replica_id": replica_id,
                            "node": node.name,
                        },
                    )
                    break

                # Fan-out: every replica of this model_key on this node.
                # node.evict's HOT->WARM demotion preserves rid, so each
                # eviction is wrapped in a drain loop until both dicts
                # have no entry for it.
                rids = sorted(
                    {
                        *node.deployments.get(model_key, {}).keys(),
                        *node.cache.get(model_key, {}).keys(),
                    }
                )
                for rid in rids:
                    dep = (
                        node.deployments.get(model_key, {}).get(rid)
                        or node.cache.get(model_key, {}).get(rid)
                    )
                    if dep is None:
                        continue
                    response.replicas.append(ReplicaState(**dep.get_state()))
                    while (
                        rid in node.deployments.get(model_key, {})
                        or rid in node.cache.get(model_key, {})
                    ):
                        node.evict(model_key, rid)
                    span.add_event(
                        "replica_evicted",
                        {
                            "model_key": model_key,
                            "replica_id": rid,
                            "node": node.name,
                        },
                    )

            return response
