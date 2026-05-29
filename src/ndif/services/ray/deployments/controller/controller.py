import asyncio
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib.metadata import distributions, packages_distributions
from typing import Any, Dict, List, Optional, Union

import ray
from pydantic import BaseModel
from ray.util.state import list_actors

from opentelemetry import trace

from .....common.logging.logger import set_logger
from .....common.metrics import (
    DeploymentGPUMetric,
    DeploymentStateMetric,
    NodeGPUMetric,
)
from .....common.providers.mailgun import MailgunProvider
from .....common.providers.objectstore import ObjectStoreProvider
from .....common.providers.socketio import SioProvider
from .....common.schema.controller import DeployResponse, ReplicaState, ReplicaStates
from .....common.schema.deployment_config import DeploymentConfig
from .....common.tracing import TracingContext, init_tracing, trace_span
from .....common.types import MODEL_KEY, REPLICA_ID
from ..modeling.base import BaseModelDeploymentArgs
from ..modeling.util import get_downloaded_models
from .cluster import Cluster, Deployment, DeploymentLevel


@dataclass
class DeploymentDelta:
    deployments_to_cache: List["Deployment"]
    deployments_from_cache: List["Deployment"]
    deployments_to_create: List[tuple[str, "Deployment"]]
    deployments_to_delete: List["Deployment"]


class _ControllerActor:
    def __init__(
        self,
        deployments: List[MODEL_KEY],
        model_import_path: str,
        default_execution_timeout_seconds: float,
        default_model_actor_class: str,
        model_cache_percentage: float,
        minimum_deployment_time_seconds: float,
        default_padding_factor: float,
        default_padding_bias: int,
    ):
        super().__init__()

        init_tracing("ndif-ray")

        self.model_import_path = model_import_path
        self.default_execution_timeout_seconds = default_execution_timeout_seconds
        self.default_model_actor_class = default_model_actor_class
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage
        self.default_padding_factor = default_padding_factor
        self.default_padding_bias = default_padding_bias
        self.runtime_context = ray.get_runtime_context()
        self.logger = set_logger("Controller")

        # Keyed by (node_id, model_key, replica_id). Each replica is tracked
        # independently so the build()/apply() diff can fire create/delete/
        # cache/from_cache per replica without confusing siblings.
        self.state: dict[tuple[str, str, str], Deployment] = dict()

        self.cluster = Cluster(
            minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
            model_cache_percentage=self.model_cache_percentage,
            default_padding_factor=self.default_padding_factor,
            default_padding_bias=self.default_padding_bias,
            default_model_actor_class=self.default_model_actor_class,
        )

        self.cluster.update_nodes()

        if deployments and deployments != [""]:
            self._deploy({key: DeploymentConfig(pinned=True) for key in deployments})

        asyncio.create_task(self.check_nodes())
        asyncio.create_task(self.emit_deployment_metrics_loop())

    def get_state(self, include_ray_state: bool = False) -> Dict[str, Any]:
        """Get the state of the controller."""

        state = {
            "cluster": self.cluster.get_state(include_ray_state=include_ray_state),
            "default_execution_timeout_seconds": self.default_execution_timeout_seconds,
            "default_model_actor_class": self.default_model_actor_class,
            "model_cache_percentage": self.model_cache_percentage,
            "minimum_deployment_time_seconds": self.minimum_deployment_time_seconds,
            "default_padding_factor": self.default_padding_factor,
            "default_padding_bias": self.default_padding_bias,
        }

        if include_ray_state:
            state["ray_dashboard_url"] = self.ray_dashboard_url
            state["runtime_context"] = self.runtime_context.get()
            state["replica_context"] = asdict(self.replica_context)
            state["serve_details"] = self.client.get_serve_details()

        state["datetime"] = datetime.now().isoformat()
        return state

    async def check_nodes(self):
        while True:
            self.cluster.update_nodes()
            await asyncio.sleep(
                int(os.environ.get("NDIF_CONTROLLER_SYNC_INTERVAL_S", "30"))
            )

    async def emit_deployment_metrics_loop(self):
        """Heartbeat the current deployment state to InfluxDB.

        Runs alongside the on-change emission in ``apply()`` so that long-lived
        deployments still produce a regular series (the join against periodic
        GPU/node profiles relies on having a recent point near any timestamp).
        """
        interval = int(
            os.environ.get("NDIF_DEPLOYMENT_METRIC_INTERVAL_S", "30")
        )
        while True:
            self._emit_deployment_metrics()
            await asyncio.sleep(interval)

    def _emit_deployment_metrics(self):
        """Emit one snapshot of the cluster's deployment state to InfluxDB.

        A pure projection of a single ``cluster.get_state()`` call (no second
        traversal of cluster internals): per live replica a ``deployment_state``
        point (what/where/sizing) + a ``deployment_gpu`` point per GPU it
        occupies (model-attributed allocation), and per (node, GPU) a
        ``node_gpu`` point (GPU resource accounting). All keyed by
        node_ip + gpu_index for joining against Ray's per-GPU profiles.

        Liveness is implicit in emission recency — the controller has already
        dropped dead/evicted/failed deployments from its state, so they stop
        appearing here. Best-effort: a metrics failure must never disrupt the
        controller, so the whole thing is guarded.
        """
        try:
            now = time.time()
            state = self.cluster.get_state()
            eval_cache = state["evaluator"]["cache"]

            for node in state["nodes"]:
                node_id = node["id"]
                node_name = node["name"]
                node_ip = node["ip"]
                resources = node["resources"]
                gpu_details = resources["gpu_details"]

                # HOT replicas only carry GPU allocations; count how many touch
                # each GPU so node_gpu reports co-location without per-replica
                # double-counting.
                replicas_per_gpu = {gpu["index"]: 0 for gpu in gpu_details}
                for deployment in node["deployments"]:
                    for gpu_index in deployment["gpus"]:
                        if gpu_index in replicas_per_gpu:
                            replicas_per_gpu[gpu_index] += 1

                for deployment in node["deployments"]:
                    self._emit_deployment(
                        deployment, node_id, node_name, node_ip, eval_cache, now
                    )
                for deployment in node["cache"]:
                    self._emit_deployment(
                        deployment, node_id, node_name, node_ip, eval_cache, now
                    )

                for gpu in gpu_details:
                    NodeGPUMetric.update(
                        node_id=node_id,
                        node_name=node_name,
                        node_ip=node_ip,
                        gpu_type=resources["gpu_type"],
                        gpu_index=gpu["index"],
                        total_memory_bytes=gpu["memory_bytes"],
                        allocated_bytes=(
                            gpu["memory_bytes"] - gpu["available_memory_bytes"]
                        ),
                        available_memory_bytes=gpu["available_memory_bytes"],
                        num_replicas=replicas_per_gpu[gpu["index"]],
                    )
        except Exception:
            self.logger.exception("Error emitting deployment metrics")

    def _emit_deployment(
        self, deployment, node_id, node_name, node_ip, eval_cache, now
    ):
        """Project one deployment.get_state() dict onto the metrics."""
        model_key = deployment["model_key"]
        replica_id = deployment["replica_id"]
        level = deployment["deployment_level"]
        gpus = deployment["gpus"]

        entry = eval_cache.get(model_key, {})

        DeploymentStateMetric.update(
            model_key=model_key,
            replica_id=replica_id,
            node_id=node_id,
            node_name=node_name,
            node_ip=node_ip,
            deployment_level=level,
            pinned=deployment["pinned"],
            actor_class=deployment["actor_class"],
            base_size_bytes=entry.get("base_size_in_bytes", 0),
            padded_size_bytes=deployment["size_bytes"],
            n_params=entry.get("n_params", 0),
            gpus=gpus,
            age_seconds=now - deployment["deployed"],
        )

        for gpu_index, allocated_bytes in gpus.items():
            DeploymentGPUMetric.update(
                model_key=model_key,
                replica_id=replica_id,
                node_id=node_id,
                node_name=node_name,
                node_ip=node_ip,
                deployment_level=level,
                gpu_index=gpu_index,
                allocated_bytes=allocated_bytes,
            )

    def _deploy(
        self,
        deployments: Union[
            MODEL_KEY, List[MODEL_KEY], Dict[MODEL_KEY, DeploymentConfig]
        ],
        trace_context: Optional[Dict[str, str]] = None,
    ) -> DeployResponse:
        configs = DeploymentConfig.normalize(deployments)

        parent_ctx = TracingContext.extract(trace_context)
        with trace_span(
            "controller.deploy",
            parent_context=parent_ctx,
            attributes={
                "ndif.model.keys": str(list(configs.keys())),
                "ndif.deploy.num_models": len(configs),
            },
        ) as span:
            self.logger.info(
                f"Deploying models: {[(key, cfg.pinned) for key, cfg in configs.items()]}"
            )

            response = self.cluster.deploy(configs)

            span.set_attribute("ndif.deploy.changed", response.change)
            for model_key, model_result in response.results.items():
                span.add_event(
                    "deploy_result",
                    {
                        "model_key": model_key,
                        "replicas": ",".join(model_result.replicas),
                        "error": model_result.error or "",
                    },
                )
            for evicted_model_key, evicted_replica_id in response.evictions:
                span.add_event(
                    "deploy_eviction",
                    {
                        "model_key": evicted_model_key,
                        "replica_id": evicted_replica_id,
                    },
                )

            if response.change:
                self.apply()

            return response

    async def deploy(
        self,
        deployments: Union[
            MODEL_KEY, List[MODEL_KEY], Dict[MODEL_KEY, DeploymentConfig]
        ],
        trace_context: Optional[Dict[str, str]] = None,
    ) -> DeployResponse:
        return self._deploy(deployments, trace_context=trace_context)

    def evict(
        self,
        model_key: MODEL_KEY,
        replica_id: Optional[REPLICA_ID] = None,
        trace_context: Optional[Dict[str, str]] = None,
    ) -> ReplicaStates:
        """Evict replicas of a model.

        If ``replica_id`` is provided, only that one replica is evicted.
        Otherwise every HOT and WARM replica of ``model_key`` is evicted.
        Returns the pre-eviction state of every replica that was evicted.
        """
        parent_ctx = TracingContext.extract(trace_context)
        with trace_span(
            "controller.evict",
            parent_context=parent_ctx,
            attributes={
                "ndif.model.key": model_key,
                "ndif.replica.id": replica_id or "",
            },
        ) as span:
            response = self.cluster.evict(model_key, replica_id)

            span.set_attribute("ndif.evict.count", len(response.replicas))

            if response.replicas:
                self.apply()

            return response

    def build(self):
        with trace_span("controller.build") as span:
            new_state = {}

            deployments_to_cache = []
            deployments_from_cache = []
            deployments_to_create = []
            deployments_to_delete = []

            # For every node
            for id, node in self.cluster.nodes.items():
                # For every cached replica
                for model_key, replica_id, cached in node.iter_cache():
                    # It will always exist in the state if its now cached.
                    existing_deployment = self.state.pop((id, model_key, replica_id))

                    # If the deployment is hot, we need to actually cache it.
                    if existing_deployment.deployment_level == DeploymentLevel.HOT:
                        deployments_to_cache.append(cached)

                    # Update state.
                    new_state[(id, model_key, replica_id)] = cached

                # For every deployed (HOT) replica
                for model_key, replica_id, deployment in node.iter_deployments():
                    existing_deployment = self.state.pop(
                        (id, model_key, replica_id), None
                    )

                    # If the deployment didn't exist before, we need to create it.
                    if existing_deployment is None:
                        deployments_to_create.append((node.name, deployment))
                    # If the deployment is warm, we need to move it from cache.
                    elif existing_deployment.deployment_level == DeploymentLevel.WARM:
                        deployments_from_cache.append(deployment)
                    # Update state.
                    new_state[(id, model_key, replica_id)] = deployment

            # For every deployment that doesn't exist in the new state, we need to delete it.
            for (id, model_key, replica_id), deployment in self.state.items():
                deployments_to_delete.append(deployment)

            # Update state.
            self.state = new_state

            delta = DeploymentDelta(
                deployments_to_cache=deployments_to_cache,
                deployments_from_cache=deployments_from_cache,
                deployments_to_create=deployments_to_create,
                deployments_to_delete=deployments_to_delete,
            )

            span.set_attribute("ndif.delta.to_cache", len(delta.deployments_to_cache))
            span.set_attribute(
                "ndif.delta.from_cache", len(delta.deployments_from_cache)
            )
            span.set_attribute("ndif.delta.to_create", len(delta.deployments_to_create))
            span.set_attribute("ndif.delta.to_delete", len(delta.deployments_to_delete))

            return delta

    def apply(self):
        with trace_span("controller.apply") as span:

            deployment_delta = self.build()

            # Delete deployments
            for deployment in deployment_delta.deployments_to_delete:
                span.add_event(
                    "deleting_deployment", {"model_key": deployment.model_key}
                )
                deployment.delete()

            # Cache deployments - must complete before from_cache can proceed to free up resources
            cache_futures = []
            cache_deployments = []
            for deployment in deployment_delta.deployments_to_cache:
                span.add_event(
                    "caching_deployment", {"model_key": deployment.model_key}
                )
                cache_future = deployment.cache()

                if cache_future is not None:
                    cache_futures.append(cache_future)
                    cache_deployments.append(deployment)
                else:
                    # cache() failed immediately - clean up
                    self.logger.error(
                        f"Failed to initiate cache for {deployment.model_key}"
                    )
                    span.add_event("cache_failed", {"model_key": deployment.model_key})
                    try:
                        deployment.delete()
                    except Exception:
                        pass
                    self._remove_deployment_from_state(deployment)

            # Wait for all cache operations to complete before proceeding
            for future, deployment in zip(cache_futures, cache_deployments):
                try:
                    ray.get(future)
                    span.add_event(
                        "cache_completed", {"model_key": deployment.model_key}
                    )
                except Exception as e:
                    self.logger.error(
                        f"Deployment {deployment.model_key} failed during cache: {e}"
                    )
                    span.add_event(
                        "cache_failed",
                        {"model_key": deployment.model_key, "error": str(e)},
                    )
                    try:
                        deployment.delete()
                    except Exception:
                        pass
                    self._remove_deployment_from_state(deployment)

            # Deploy models from cache - spawn monitoring tasks
            for deployment in deployment_delta.deployments_from_cache:
                span.add_event(
                    "restoring_from_cache", {"model_key": deployment.model_key}
                )
                future = deployment.from_cache()
                if future is not None:
                    asyncio.create_task(
                        self._monitor_deployment(future, deployment, "from_cache")
                    )
                else:
                    # from_cache() failed immediately - clean up
                    self.logger.error(
                        f"Failed to initiate from_cache for {deployment.model_key}"
                    )
                    span.add_event(
                        "from_cache_failed", {"model_key": deployment.model_key}
                    )
                    deployment.delete()
                    self._remove_deployment_from_state(deployment)

            # Create models from disk - spawn monitoring tasks
            for name, deployment in deployment_delta.deployments_to_create:
                span.add_event(
                    "creating_deployment",
                    {"model_key": deployment.model_key, "node": name},
                )
                execution_timeout = (
                    deployment.execution_timeout_seconds
                    if deployment.execution_timeout_seconds is not None
                    else self.default_execution_timeout_seconds
                )
                deployment_args = BaseModelDeploymentArgs(
                    model_key=deployment.model_key,
                    execution_timeout=execution_timeout,
                )

                # create() returns None always, but may fail internally
                deployment.create(name, deployment_args)

                # Get the actor handle and monitor its ready state
                try:
                    actor = deployment.actor
                    ready_future = actor.__ray_ready__.remote()
                    asyncio.create_task(
                        self._monitor_deployment(ready_future, deployment, "create")
                    )
                except Exception as e:
                    # create() failed or actor not available - clean up
                    self.logger.error(
                        f"Failed to get actor handle for {deployment.model_key}: {e}"
                    )
                    span.add_event(
                        "create_failed",
                        {"model_key": deployment.model_key, "error": str(e)},
                    )
                    deployment.delete()
                    self._remove_deployment_from_state(deployment)

            # Snapshot the post-apply deployment state so transitions are
            # captured immediately, not just at the next heartbeat. Note:
            # from_cache/create complete asynchronously, so this reflects the
            # intended state — the heartbeat reconciles the steady state.
            self._emit_deployment_metrics()

    async def _monitor_deployment(
        self,
        future: ray.ObjectRef,
        deployment: "Deployment",
        operation: str,
    ) -> None:
        """Monitor a deployment future and clean up on failure.

        This runs as an async task, so it doesn't block the controller.

        Args:
            future: Ray future to monitor.
            deployment: The Deployment object being monitored.
            operation: Name of the operation for logging.
        """
        with trace_span(
            "controller.monitor_deployment",
            attributes={
                "ndif.model.key": deployment.model_key,
                "ndif.deploy.operation": operation,
            },
        ) as span:
            try:
                span.add_event("waiting_for_ray_actor")
                # Use asyncio to wait for the ray future without blocking
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: ray.get(future)
                )
                span.add_event("ray_actor_ready")
            except Exception as e:
                span.set_status(trace.StatusCode.ERROR, str(e))
                span.record_exception(e)
                self.logger.error(
                    f"Deployment {deployment.model_key} failed during {operation}: {e}"
                )
                # Delete the failed deployment to return resources
                # Wrap in try-catch as the actor may already be gone
                try:
                    deployment.delete()
                except Exception as delete_error:
                    self.logger.debug(
                        f"Error deleting failed deployment {deployment.model_key}: {delete_error}"
                    )
                self._remove_deployment_from_state(deployment)

    def _remove_deployment_from_state(self, deployment: "Deployment") -> None:
        """Remove a deployment from the internal state.

        Args:
            deployment: The deployment to remove.
        """
        # Remove from state using node_id + replica_id
        state_key = (deployment.node_id, deployment.model_key, deployment.replica_id)
        if state_key in self.state:
            del self.state[state_key]

        # Remove from the specific cluster node using node_id + replica_id
        if deployment.node_id and deployment.node_id in self.cluster.nodes:
            node = self.cluster.nodes[deployment.node_id]
            replicas = node.deployments.get(deployment.model_key)
            if replicas is not None and deployment.replica_id in replicas:
                # Return GPUs to the node
                node.gpu_resources.release(deployment.gpus)
                del replicas[deployment.replica_id]
                if not replicas:
                    del node.deployments[deployment.model_key]
            cached_replicas = node.cache.get(deployment.model_key)
            if cached_replicas is not None and deployment.replica_id in cached_replicas:
                # Return CPU memory to the node
                node.cpu_resources.release(deployment.size_bytes)
                del cached_replicas[deployment.replica_id]
                if not cached_replicas:
                    del node.cache[deployment.model_key]

    def get_deployment(
        self,
        model_key: MODEL_KEY,
        replica_id: Optional[REPLICA_ID] = None,
    ) -> ReplicaStates:
        """List HOT deployments matching the query.

        Always returns a ReplicaStates wrapper, even if the caller scoped to
        a specific replica (in which case the list is 0 or 1 entries long).
        WARM (cached) replicas are not included — callers wanting to inspect
        the cache should use the full cluster state.
        """
        response = ReplicaStates()
        for node in self.cluster.nodes.values():
            replicas = node.deployments.get(model_key, {})
            if replica_id is not None:
                deployment = replicas.get(replica_id)
                if deployment is not None:
                    response.replicas.append(
                        ReplicaState(**deployment.get_state())
                    )
            else:
                for deployment in replicas.values():
                    response.replicas.append(
                        ReplicaState(**deployment.get_state())
                    )
        return response

    def env(self) -> Dict[str, Any]:
        """Get the Python environment information.

        Returns:
            Dictionary containing Python version and installed pip packages.
        """
        pd_map = packages_distributions()
        dist_to_imports = {}
        for import_name, dist_names in pd_map.items():
            for dist_name in dist_names:
                if dist_name not in dist_to_imports:
                    dist_to_imports[dist_name] = []
                dist_to_imports[dist_name].append(import_name)

        packages = {}
        for dist in distributions():
            dist_name = dist.metadata["Name"]
            version = dist.version

            # Get import names from packages_distributions mapping
            import_names = dist_to_imports.get(dist_name, [])

            if import_names:
                for imp_name in import_names:
                    packages[imp_name] = version
            else:
                # Fallback to distribution name if no import mapping found
                packages[dist_name] = version

        return {
            "python_version": sys.version,
            "packages": packages,
        }

    def status(self):
        ray_status = list_actors()

        status = {}

        # A single ModelActor name can have multiple Ray entries — every
        # restart (ray.kill(no_restart=False), CUDA self-kill, etc.) leaves
        # a DEAD record behind alongside the new ALIVE one. The previous
        # loop just overwrote on each iteration, so whichever entry came
        # last won — typically a stale DEAD record, leaving the dashboard
        # showing "UNHEALTHY" for a perfectly healthy deployment.
        #
        # Collapse to the healthiest state for each name. RUNNING beats
        # DEPLOYING beats UNHEALTHY — once a successor actor is up, the
        # ghosts of its predecessors don't matter to the user.
        STATE_PRIORITY = {"RUNNING": 0, "DEPLOYING": 1, "UNHEALTHY": 2}

        for actor_state in ray_status:
            # Actor names are now "{replica_id}:ModelActor:{model_key}".
            # Match by substring so existing ModelActor: prefixed names from a
            # previous release would also be picked up if they linger.
            if ":ModelActor:" not in actor_state.name and not actor_state.name.startswith(
                "ModelActor:"
            ):
                continue
            if actor_state.state in {
                "DEPENDENCIES_UNREADY",
                "PENDING_CREATION",
                "RESTARTING",
            }:
                application_state = "DEPLOYING"
            elif actor_state.state == "ALIVE":
                application_state = "RUNNING"
            elif actor_state.state == "DEAD":
                application_state = "UNHEALTHY"
            else:
                continue

            existing = status.get(actor_state.name, {}).get("application_state")
            if existing is None or STATE_PRIORITY[application_state] < STATE_PRIORITY[existing]:
                status[actor_state.name] = {
                    "application_state": application_state,
                }

        existing_repo_ids = set()

        for node in self.cluster.nodes.values():
            for _, _, deployment in node.iter_deployments():
                application_name = deployment.name

                if application_name not in status:
                    continue

                if deployment.model_key not in self.cluster.evaluator.cache:
                    continue

                entry = self.cluster.evaluator.cache[deployment.model_key]

                # Mirror the actor_class normalization in Deployment.get_state:
                # accept dotted-path strings as-is; render decorated classes
                # as ``module.qualname``; ``None`` → ``None``.
                if deployment.actor_class is None:
                    actor_class_repr = None
                elif isinstance(deployment.actor_class, str):
                    actor_class_repr = deployment.actor_class
                else:
                    actor_class_repr = (
                        f"{deployment.actor_class.__module__}."
                        f"{deployment.actor_class.__qualname__}"
                    )

                status[application_name] = {
                    **status[application_name],
                    "deployment_level": deployment.deployment_level.name,
                    "pinned": deployment.pinned,
                    "model_key": deployment.model_key,
                    "replica_id": deployment.replica_id,
                    "repo_id": entry.config._name_or_path,
                    "revision": entry.revision,
                    "config": entry.config.to_json_string(),
                    "n_params": entry.n_params,
                    "size_bytes": deployment.size_bytes,
                    "actor_class": actor_class_repr,
                }

                if (
                    not deployment.pinned
                    and self.minimum_deployment_time_seconds is not None
                ):
                    status[application_name]["schedule"] = {
                        "end_time": deployment.end_time(
                            self.minimum_deployment_time_seconds
                        ),
                    }

                existing_repo_ids.add(entry.config._name_or_path)

            for _, _, cached_deployment in node.iter_cache():
                application_name = cached_deployment.name

                if application_name not in status:
                    continue

                if cached_deployment.model_key not in self.cluster.evaluator.cache:
                    continue

                entry = self.cluster.evaluator.cache[cached_deployment.model_key]

                status[application_name] = {
                    "deployment_level": DeploymentLevel.WARM.name,
                    "model_key": cached_deployment.model_key,
                    "replica_id": cached_deployment.replica_id,
                    "repo_id": entry.config._name_or_path,
                    "revision": entry.revision,
                    "config": entry.config.to_json_string(),
                    "n_params": entry.n_params,
                }

                existing_repo_ids.add(entry.config._name_or_path)

        downloaded_models = get_downloaded_models()

        for repo_id in downloaded_models:
            if repo_id not in existing_repo_ids:
                status[repo_id] = {
                    "deployment_level": DeploymentLevel.COLD.name,
                    "repo_id": repo_id,
                }

        return {
            "deployments": status,
            "cluster": {
                "nodes": {
                    node_id: {
                        "resources": {
                            "gpu_details": [
                                {
                                    "index": gpu.index,
                                    "memory_bytes": gpu.memory_bytes,
                                    "available_memory_bytes": gpu.available_memory_bytes,
                                }
                                for gpu in node.gpu_resources.gpus
                            ],
                        },
                        "deployments": {
                            model_key: {
                                replica_id: {"gpus": deployment.gpus}
                                for replica_id, deployment in replicas.items()
                            }
                            for model_key, replicas in node.deployments.items()
                        },
                    }
                    for node_id, node in self.cluster.nodes.items()
                }
            },
        }


@ray.remote(num_cpus=1, num_gpus=0, max_restarts=-1, resources={"head": 1})
class ControllerActor(_ControllerActor):
    pass


class ControllerDeploymentArgs(BaseModel):
    deployments: List[MODEL_KEY] = os.environ.get("NDIF_DEPLOYMENTS", "").split("|")

    model_import_path: str = "ndif.services.ray.deployments.modeling.model:app"
    default_execution_timeout_seconds: Optional[float] = float(
        os.environ.get("NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS", "3600")
    )
    default_model_actor_class: str = os.environ.get(
        "NDIF_DEFAULT_MODEL_ACTOR_CLASS",
        "ndif.services.ray.deployments.modeling.base.ModelActor",
    )
    minimum_deployment_time_seconds: Optional[float] = float(
        os.environ.get("NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS", "3600")
    )
    model_cache_percentage: Optional[float] = float(
        os.environ.get("NDIF_MODEL_CACHE_PERCENTAGE", "0.9")
    )
    default_padding_factor: Optional[float] = float(
        os.environ.get("NDIF_DEFAULT_PADDING_FACTOR", "0.15")
    )
    default_padding_bias: Optional[int] = int(
        os.environ.get("NDIF_DEFAULT_PADDING_BIAS", str(500 * 1024 * 1024))
    )


def app(**kwargs):
    args = ControllerDeploymentArgs(**kwargs)

    actor = ControllerActor.options(
        name="Controller",
        namespace="NDIF",
        lifetime="detached",
        runtime_env={
            **SioProvider.to_env(),
            **ObjectStoreProvider.to_env(),
            **MailgunProvider.to_env(),
        },
    ).remote(**args.model_dump())
