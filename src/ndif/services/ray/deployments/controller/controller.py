import asyncio
import importlib
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import distributions, packages_distributions
from typing import Any, Dict, List, Optional, Union

import ray
from pydantic import BaseModel
from ray.util.state import list_actors

from .....common.schema.controller import (
    DeploymentConfig,
    DeployResponse,
    ReplicaState,
    ReplicaStates,
)
from .....common.types import MODEL_KEY, REPLICA_ID
from ..modeling.base import BaseModelDeploymentArgs
from ..modeling.util import get_downloaded_models
from .cluster import Cluster, Deployment, DeploymentLevel

logger = logging.getLogger("ndif.controller")


def _import_from_path(path: str):
    """Import and return the object named by a dotted path (``pkg.mod.Name``)."""
    module_path, _, attr = path.rpartition(".")
    return getattr(importlib.import_module(module_path), attr)


@dataclass
class DeploymentDelta:
    deployments_to_cache: List["Deployment"]
    deployments_from_cache: List["Deployment"]
    deployments_to_create: List[tuple[str, "Deployment"]]
    deployments_to_delete: List["Deployment"]


class _ControllerActor:
    """Reconciles desired model deployments against live Ray actors.

    The cluster decides *where* replicas go (placement, eviction, HOT/WARM/COLD
    caching); this actor turns those decisions into Ray actor lifecycle ops via
    a build()/apply() diff, and exposes the deploy/evict/get_deployment API the
    queue uses (plus status/env for the dashboard and CLI).
    """

    def __init__(
        self,
        deployments: List[MODEL_KEY],
        model_import_path: str,
        controller_import_path: str,
        default_execution_timeout_seconds: float,
        model_cache_percentage: float,
        minimum_deployment_time_seconds: float,
        default_padding_factor: float,
        default_padding_bias: int,
        tp_model_actor_class: Optional[str] = None,
        default_dtype: str = "bfloat16",
    ):
        super().__init__()

        # Install the Loki log handler in this actor's worker process (each Ray
        # actor is its own process, so it must be set up here, not on the driver).
        from .....common.providers.loki import LokiProvider

        LokiProvider.connect()

        self.model_import_path = model_import_path
        self.controller_import_path = controller_import_path
        self.default_execution_timeout_seconds = default_execution_timeout_seconds
        # The model actor class every deployment is built from (a per-deployment
        # DeploymentConfig.actor_class overrides it). Resolved to a concrete
        # class downstream in Deployment.create.
        self.default_model_actor_class = model_import_path
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage
        self.default_padding_factor = default_padding_factor
        self.default_padding_bias = default_padding_bias
        self.tp_model_actor_class = tp_model_actor_class
        self.default_dtype = default_dtype
        self.runtime_context = ray.get_runtime_context()

        # Keyed by (node_id, model_key, replica_id). Each replica is tracked
        # independently so the build()/apply() diff can fire create/delete/
        # cache/from_cache per replica without confusing siblings.
        self.state: dict[tuple[str, str, str], Deployment] = dict()

        self.cluster = Cluster(
            minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
            model_cache_percentage=self.model_cache_percentage,
            default_padding_factor=self.default_padding_factor,
            default_padding_bias=self.default_padding_bias,
            tp_model_actor_class=self.tp_model_actor_class,
            default_model_actor_class=self.default_model_actor_class,
        )

        # TODO: detached model actors from a previous controller outlive a
        # controller restart but aren't re-adopted here — the rebuilt cluster
        # starts empty and can re-place onto GPUs those orphans still hold,
        # over-committing memory. Reconcile against live ModelActors on startup.
        self.cluster.update_nodes()

        if deployments and deployments != [""]:
            self._deploy({key: DeploymentConfig(pinned=True) for key in deployments})

        asyncio.create_task(self.check_nodes())

    def get_state(self, include_ray_state: bool = False) -> Dict[str, Any]:
        state = {
            "cluster": self.cluster.get_state(include_ray_state=include_ray_state),
            "default_execution_timeout_seconds": self.default_execution_timeout_seconds,
            "default_model_actor_class": self.default_model_actor_class,
            "model_cache_percentage": self.model_cache_percentage,
            "minimum_deployment_time_seconds": self.minimum_deployment_time_seconds,
            "default_padding_factor": self.default_padding_factor,
            "default_padding_bias": self.default_padding_bias,
            "tp_model_actor_class": self.tp_model_actor_class,
            "runtime_context": self.runtime_context.get(),
            "datetime": datetime.now().isoformat(),
        }
        return state

    async def check_nodes(self):
        # TODO: this reconciles *nodes* against reality but never *actors*, and
        # a model actor that dies permanently after a successful deploy wedges
        # its model until an operator intervenes. `_monitor_deployment` only
        # awaits the initial deploy future, so once a deployment succeeds
        # nothing looks at it again; the record (and its GPU reservation)
        # survives the actor. `get_deployment` then keeps handing that dead
        # replica id to the queue, `Processor.start` adopts it and awaits
        # `Replica.wait`, and that wait neither returns nor raises — so the
        # healthy replicas behind it in the list are never started, the purge
        # that would error the queued users is never reached, and every request
        # for the model hangs with no error and no timeout. Observed: a
        # `ray.kill(no_restart=True)` on an 8B replica left the model
        # permanently unusable, with the controller reporting 29.0/47.4 GB free
        # against 620 MiB actually in use. Neither a redeploy nor an API restart
        # recovers it (the fresh Processor re-adopts the same dead id); only
        # `ndif evict` clears the record.
        #
        # Reaping here would fix it at the source, and the cleanup already
        # exists: `_remove_deployment_from_state` drops the entry *and* releases
        # the node's gpu/cpu resources. Two things to get right:
        #   - key off Ray's actor state, not a name lookup failing. A recoverable
        #     restart reports RESTARTING (never DEAD) and its record must be left
        #     alone or the dispatch-side restart wait breaks; a name that doesn't
        #     resolve is also the normal PENDING_CREATION window.
        #   - drop only, and let demand re-provision. Re-provisioning from a
        #     heartbeat turns a model that reliably OOMs on load into a flap loop.
        #
        # This prevents the wedge but cannot cure one: `Replica.wait` is pinned
        # to a replica id, so forgetting the record here won't wake a Processor
        # already stuck on it. That needs a bound on the adoption wait in
        # `Processor.start` — deliberately unlike the dispatch-side wait, which
        # is unbounded because there the actor is known to be coming back.
        while True:
            self.cluster.update_nodes()
            await asyncio.sleep(
                int(os.environ.get("NDIF_CONTROLLER_SYNC_INTERVAL_S", "30"))
            )

    def _deploy(
        self,
        deployments: Union[
            MODEL_KEY, List[MODEL_KEY], Dict[MODEL_KEY, DeploymentConfig]
        ],
    ) -> DeployResponse:
        configs = DeploymentConfig.normalize(deployments)

        # Pin each config's dtype to a concrete value now, so the evaluator's
        # size estimate and the actor's load use the *same* dtype (the memory
        # accounting that places a replica must match what it actually loads).
        for config in configs.values():
            if config.dtype is None:
                config.dtype = self.default_dtype

        logger.info(
            f"Deploying models: "
            f"{[(key, cfg.pinned) for key, cfg in configs.items()]}"
        )

        response = self.cluster.deploy(configs)

        if response.change:
            self.apply()

        return response

    def live(self, model_key: MODEL_KEY) -> List["Deployment"]:
        """Every replica of ``model_key`` the cluster is currently running."""
        return [
            deployment
            for node in self.cluster.nodes.values()
            for deployment in node.deployments.get(model_key, {}).values()
        ]

    def _like_existing(
        self, model_key: MODEL_KEY, config: DeploymentConfig
    ) -> DeploymentConfig:
        """``config`` with its unset fields filled in from a replica already running.

        Adding capacity to a model should add *more of what is there*, not a
        second opinion about how the model should be served. Two replicas under
        one model key answering differently is the failure this exists for: a
        sharded replica and an unsharded one give different numbers for the same
        block (measured: 424.7618 against 424.2299) and nothing tells the caller
        which one answered.

        The live cluster is the source of truth rather than a remembered config,
        so there is nothing to persist, nothing to expire, and no way for this to
        change what an explicit value means -- a field the caller set always wins,
        and is also what makes a replica "matching" enough to copy the rest from.

        With no live replica there is nothing to copy and the config is returned
        untouched: at cold start nothing in the cluster claims the model is served
        any particular way, and the evaluator decides as it always has. A durable
        answer to that belongs in a config file, not here.
        """
        candidates = self.live(model_key)
        if not candidates:
            return config

        # Prefer a replica that agrees with everything the caller pinned; among
        # equals the choice is arbitrary, because replicas of one model are meant
        # to be alike and a disagreement is worth surfacing rather than resolving.
        def agrees(deployment: "Deployment") -> bool:
            if config.actor_class is not None and deployment.actor_class != config.actor_class:
                return False
            if config.dtype is not None and deployment.dtype != config.dtype:
                return False
            if config.gpus is not None and len(deployment.gpus) != config.gpus:
                return False
            return True

        model = next((d for d in candidates if agrees(d)), candidates[0])

        filled = config.model_copy(deep=True)
        if filled.actor_class is None:
            filled.actor_class = model.actor_class
        if filled.dtype is None:
            filled.dtype = model.dtype
        if filled.gpus is None:
            filled.gpus = len(model.gpus)
        if filled.execution_timeout_seconds is None:
            filled.execution_timeout_seconds = model.execution_timeout_seconds
        # Deliberately *not* size_bytes. A Deployment's is the evaluator's output
        # -- already padded -- while the config field means the model's own
        # weights, with padding applied on top. Copying one into the other pads
        # twice: measured on a 2-GPU replica, 33.6 GB per card became 70.9 GB.
        # There is nothing to gain either way, since the evaluator sizes the new
        # replica from the same dtype and the same defaults as the old one.
        return filled

    def scale(
        self,
        model_key: MODEL_KEY,
        n: int = 1,
        config: Optional[DeploymentConfig] = None,
    ) -> DeployResponse:
        """Add ``n`` replicas of ``model_key``, like the ones already running.

        Additive, like ``deploy``: ``n`` is how many to add, not a target. The
        difference from ``deploy`` is where the unspecified fields come from --
        here, from a live replica (:meth:`_like_existing`) instead of the
        controller's defaults, so growing a tensor-parallel model gives you more
        tensor-parallel replicas.
        """
        config = (config or DeploymentConfig()).model_copy(deep=True)
        config.replicas = n
        return self._deploy({model_key: self._like_existing(model_key, config)})

    async def deploy(
        self,
        deployments: Union[
            MODEL_KEY, List[MODEL_KEY], Dict[MODEL_KEY, DeploymentConfig]
        ],
    ) -> DeployResponse:
        await self._describe(deployments)
        return self._deploy(deployments)

    async def _describe(
        self,
        deployments: Union[
            MODEL_KEY, List[MODEL_KEY], Dict[MODEL_KEY, DeploymentConfig]
        ],
    ) -> None:
        """Warm the evaluator's cache for these models, off the event loop.

        Sizing a model reaches the Hub, and placement is synchronous on this
        loop — so without this, deploying a model the evaluator has not seen
        parks `check_nodes`, every in-flight `_monitor_deployment` and every
        other RPC for as long as the network takes. Bounded now (nnsight gives up
        on a slow Hub), but bounded is not the same as not blocking.

        Deliberately *only* the reads. The placement that follows still runs on
        the loop, one deploy at a time, exactly as before — moving that to a
        thread would let it interleave with `check_nodes` over the same cluster
        state, which is a much larger change than this problem is worth.
        """
        configs = DeploymentConfig.normalize(deployments)
        if not configs:
            return

        await asyncio.gather(
            *(
                asyncio.to_thread(
                    self.cluster.evaluator.prefetch,
                    model_key,
                    config.dtype or self.default_dtype,
                    config.trusted,
                )
                for model_key, config in configs.items()
            )
        )

    def evict(
        self,
        model_key: MODEL_KEY,
        replica_id: Optional[REPLICA_ID] = None,
    ) -> ReplicaStates:
        """Evict replicas of a model.

        With ``replica_id``, only that one is evicted; otherwise every HOT and
        WARM replica of ``model_key``. Returns the pre-eviction state of every
        replica that was evicted.
        """
        response = self.cluster.evict(model_key, replica_id)

        if response.replicas:
            self.apply()

        return response

    def build(self) -> DeploymentDelta:
        """Diff the live cluster against the last-applied state.

        Walks each node's cache/deployments and classifies every replica into
        create / from_cache / to_cache, with whatever's left in the old state
        being to_delete. Updates ``self.state`` to the new truth.
        """
        new_state = {}

        deployments_to_cache = []
        deployments_from_cache = []
        deployments_to_create = []
        deployments_to_delete = []

        for id, node in self.cluster.nodes.items():
            # Every cached (WARM) replica.
            for model_key, replica_id, cached in node.iter_cache():
                # It will always exist in state if it's now cached.
                existing_deployment = self.state.pop((id, model_key, replica_id))

                if existing_deployment.deployment_level == DeploymentLevel.HOT:
                    deployments_to_cache.append(cached)

                new_state[(id, model_key, replica_id)] = cached

            # Every deployed (HOT) replica.
            for model_key, replica_id, deployment in node.iter_deployments():
                existing_deployment = self.state.pop((id, model_key, replica_id), None)

                if existing_deployment is None:
                    deployments_to_create.append((node.name, deployment))
                elif existing_deployment.deployment_level == DeploymentLevel.WARM:
                    deployments_from_cache.append(deployment)

                new_state[(id, model_key, replica_id)] = deployment

        # Anything left in the old state no longer exists -> delete.
        for (id, model_key, replica_id), deployment in self.state.items():
            deployments_to_delete.append(deployment)

        self.state = new_state

        return DeploymentDelta(
            deployments_to_cache=deployments_to_cache,
            deployments_from_cache=deployments_from_cache,
            deployments_to_create=deployments_to_create,
            deployments_to_delete=deployments_to_delete,
        )

    def apply(self):
        """Execute the build() delta as Ray actor lifecycle operations."""
        deployment_delta = self.build()

        # Delete.
        for deployment in deployment_delta.deployments_to_delete:
            deployment.delete()

        # Cache (HOT->WARM): must finish before from_cache frees its resources.
        cache_futures = []
        cache_deployments = []
        for deployment in deployment_delta.deployments_to_cache:
            cache_future = deployment.cache()
            if cache_future is not None:
                cache_futures.append(cache_future)
                cache_deployments.append(deployment)
            else:
                logger.error(f"Failed to initiate cache for {deployment.model_key}")
                try:
                    deployment.delete()
                except Exception:
                    pass
                self._remove_deployment_from_state(deployment)

        for future, deployment in zip(cache_futures, cache_deployments):
            try:
                ray.get(future)
            except Exception as e:
                logger.error(
                    f"Deployment {deployment.model_key} failed during cache: {e}"
                )
                try:
                    deployment.delete()
                except Exception:
                    pass
                self._remove_deployment_from_state(deployment)

        # Restore from cache (WARM->HOT): monitor asynchronously.
        for deployment in deployment_delta.deployments_from_cache:
            future = deployment.from_cache()
            if future is not None:
                asyncio.create_task(
                    self._monitor_deployment(future, deployment, "from_cache")
                )
            else:
                logger.error(
                    f"Failed to initiate from_cache for {deployment.model_key}"
                )
                deployment.delete()
                self._remove_deployment_from_state(deployment)

        # Create from disk: monitor the actor's readiness asynchronously.
        for name, deployment in deployment_delta.deployments_to_create:
            execution_timeout = (
                deployment.execution_timeout_seconds
                if deployment.execution_timeout_seconds is not None
                else self.default_execution_timeout_seconds
            )
            deployment_args = BaseModelDeploymentArgs(
                model_key=deployment.model_key,
                execution_timeout=execution_timeout,
                dtype=deployment.dtype or self.default_dtype,
                trust_remote_code=deployment.trusted,
            )

            deployment.create(name, deployment_args)

            try:
                actor = deployment.actor
                ready_future = actor.__ray_ready__.remote()
                asyncio.create_task(
                    self._monitor_deployment(ready_future, deployment, "create")
                )
            except Exception as e:
                logger.error(
                    f"Failed to get actor handle for {deployment.model_key}: {e}"
                )
                deployment.delete()
                self._remove_deployment_from_state(deployment)

    async def _monitor_deployment(
        self,
        future: ray.ObjectRef,
        deployment: "Deployment",
        operation: str,
    ) -> None:
        """Await a deployment future off the main path; clean up on failure.

        Awaiting the ObjectRef directly is valid in an async actor and
        non-blocking — no thread parked for the (possibly minutes-long) op. On
        failure, return the resources and prune our internal state.
        """
        try:
            await future
        except Exception as e:
            logger.error(
                f"Deployment {deployment.model_key} failed during {operation}: {e}"
            )
            try:
                deployment.delete()
            except Exception as delete_error:
                logger.debug(
                    f"Error deleting failed deployment {deployment.model_key}: "
                    f"{delete_error}"
                )
            self._remove_deployment_from_state(deployment)

    def _remove_deployment_from_state(self, deployment: "Deployment") -> None:
        """Drop a deployment from internal state and return its node resources."""
        state_key = (deployment.node_id, deployment.model_key, deployment.replica_id)
        if state_key in self.state:
            del self.state[state_key]

        if deployment.node_id and deployment.node_id in self.cluster.nodes:
            node = self.cluster.nodes[deployment.node_id]
            replicas = node.deployments.get(deployment.model_key)
            if replicas is not None and deployment.replica_id in replicas:
                node.gpu_resources.release(deployment.gpus)
                del replicas[deployment.replica_id]
                if not replicas:
                    del node.deployments[deployment.model_key]
            cached_replicas = node.cache.get(deployment.model_key)
            if cached_replicas is not None and deployment.replica_id in cached_replicas:
                node.cpu_resources.release(deployment.size_bytes)
                del cached_replicas[deployment.replica_id]
                if not cached_replicas:
                    del node.cache[deployment.model_key]

    def get_deployment(
        self,
        model_key: MODEL_KEY,
        replica_id: Optional[REPLICA_ID] = None,
    ) -> ReplicaStates:
        """List HOT replicas matching the query (WARM/cached are not included)."""
        response = ReplicaStates()
        for node in self.cluster.nodes.values():
            replicas = node.deployments.get(model_key, {})
            if replica_id is not None:
                deployment = replicas.get(replica_id)
                if deployment is not None:
                    response.replicas.append(ReplicaState(**deployment.get_state()))
            else:
                for deployment in replicas.values():
                    response.replicas.append(ReplicaState(**deployment.get_state()))
        return response

    def env(self) -> Dict[str, Any]:
        """Python version + installed pip packages (for the CLI to match envs)."""
        pd_map = packages_distributions()
        dist_to_imports = {}
        for import_name, dist_names in pd_map.items():
            for dist_name in dist_names:
                dist_to_imports.setdefault(dist_name, []).append(import_name)

        packages = {}
        for dist in distributions():
            dist_name = dist.metadata["Name"]
            version = dist.version
            import_names = dist_to_imports.get(dist_name, [])
            if import_names:
                for imp_name in import_names:
                    packages[imp_name] = version
            else:
                packages[dist_name] = version

        return {"python_version": sys.version, "packages": packages}

    def status(self):
        """Dashboard view: per-deployment health + cluster resources + COLD models."""
        ray_status = list_actors()

        status = {}

        # One ModelActor name can have multiple Ray entries (restarts leave DEAD
        # records beside the ALIVE one). Collapse to the healthiest state.
        STATE_PRIORITY = {"RUNNING": 0, "DEPLOYING": 1, "UNHEALTHY": 2}

        for actor_state in ray_status:
            if (
                ":ModelActor:" not in actor_state.name
                and not actor_state.name.startswith("ModelActor:")
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
            if (
                existing is None
                or STATE_PRIORITY[application_state] < STATE_PRIORITY[existing]
            ):
                status[actor_state.name] = {"application_state": application_state}

        existing_repo_ids = set()

        for node in self.cluster.nodes.values():
            for _, _, deployment in node.iter_deployments():
                application_name = deployment.name
                if application_name not in status:
                    continue
                if deployment.model_key not in self.cluster.evaluator.cache:
                    continue

                entry = self.cluster.evaluator.cache[deployment.model_key]

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
                    "trusted": deployment.trusted,
                    "dtype": deployment.dtype,
                    "execution_timeout_seconds": deployment.execution_timeout_seconds,
                    "model_key": deployment.model_key,
                    "replica_id": deployment.replica_id,
                    "repo_id": getattr(entry.config, "_name_or_path", None),
                    "revision": entry.revision,
                    "config": (
                        entry.config.to_json_string()
                        if entry.config is not None
                        else None
                    ),
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

                existing_repo_ids.add(getattr(entry.config, "_name_or_path", None))

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
                    "repo_id": getattr(entry.config, "_name_or_path", None),
                    "revision": entry.revision,
                    "config": (
                        entry.config.to_json_string()
                        if entry.config is not None
                        else None
                    ),
                    "n_params": entry.n_params,
                }

                existing_repo_ids.add(getattr(entry.config, "_name_or_path", None))

        # Drop orphaned dead actors. Ray keeps a DEAD record for every actor it
        # has ever run, so each evict/restart leaves one behind forever; the
        # loops above only enrich names the controller still tracks, so an
        # orphan stays a bare {"application_state": "UNHEALTHY"} with no
        # repo_id. Left in, they accumulate monotonically — one per teardown,
        # never reaped — and on a cluster that swaps models routinely they come
        # to outnumber the real entries (9 ghosts to 4 real ones after a single
        # day of testing), breaking any consumer that reads `deployments` and
        # expects a repo_id.
        #
        # Only *orphans* are dropped. A dead actor the controller still tracks
        # did get enriched, and is worth surfacing: it means the controller
        # believes a replica is deployed while its actor is gone.
        status = {
            name: entry
            for name, entry in status.items()
            if not (
                entry.get("application_state") == "UNHEALTHY"
                and entry.get("model_key") is None
            )
        }

        for repo_id in get_downloaded_models():
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

    # Dotted import path of the model actor class the controller instantiates
    # per deployment (a per-deployment DeploymentConfig.actor_class overrides
    # it). NDIF_DEFAULT_MODEL_ACTOR_CLASS is still honored so an environment
    # that points it at the sandboxed actor keeps working unchanged.
    model_import_path: str = os.environ.get(
        "NDIF_MODEL_IMPORT_PATH",
        os.environ.get(
            "NDIF_DEFAULT_MODEL_ACTOR_CLASS",
            "ndif.services.ray.deployments.modeling.base.ModelActor",
        ),
    )
    # Dotted import path of the actor class a *tensor-parallel* replica gets --
    # chosen automatically when a model needs more than one GPU and shards evenly
    # into exactly that many (see the evaluator).
    #
    # **Unset means tensor parallelism is off**, not "use the built-in one": no
    # degree is worked out, no GPU count is rounded up to a shardable one, and
    # every multi-GPU model is spread layer-by-layer with accelerate. Set it to
    # `ndif.services.ray.tp.model.TPModelActor` to turn the feature on, or to
    # `ndif.services.ray.tp.model.SandboxedTPModelActor` on a cluster that also
    # takes untrusted traffic. Opt-in because a sharded replica cannot be
    # cached and needs transformers >= 5.15 to shard correctly.
    tp_model_actor_class: Optional[str] = os.environ.get(
        "NDIF_TP_MODEL_ACTOR_CLASS"
    ) or None
    # Dotted import path of the controller actor class app() launches. Lets an
    # operator swap in a subclass without editing this module.
    controller_import_path: str = os.environ.get(
        "NDIF_CONTROLLER_IMPORT_PATH",
        "ndif.services.ray.deployments.controller.controller.ControllerActor",
    )
    # Unset by default: no execution cap, so a legitimately long job is never
    # cut off on someone's own deployment. Set the env var (or a per-model
    # ``execution_timeout_seconds``) on anything shared — without a cap a
    # runaway block holds its replica indefinitely, and with a small
    # ``NDIF_AUTOSCALING_MAX_REPLICAS`` a few of those take the model out.
    _execution_timeout_env: Optional[str] = os.environ.get(
        "NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS"
    )
    default_execution_timeout_seconds: Optional[float] = (
        float(_execution_timeout_env) if _execution_timeout_env else None
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
    default_dtype: str = os.environ.get("NDIF_DEFAULT_DTYPE", "bfloat16")
    # Largest result, after compression, a model actor hands back on the
    # COMPLETED response instead of staging in the object store for the client
    # to download. Unset means no cap, so every result for a blocking request
    # goes over the websocket. Declared here with the rest of the actor
    # configuration and forwarded into each actor's env (see
    # `cluster.deployment`), because the actor reads it from its environment.
    #
    # Redis is the ceiling worth knowing: a pubsub client whose output buffer
    # exceeds `client-output-buffer-limit pubsub` (32 MB hard by default) is
    # disconnected and the response goes with it. A value that is not a positive
    # integer is treated as unset.
    _max_socket_result_env: Optional[str] = os.environ.get(
        "NDIF_MAX_SOCKET_RESULT_BYTES"
    )
    max_socket_result_bytes: Optional[int] = (
        int(_max_socket_result_env)
        if _max_socket_result_env and _max_socket_result_env.lstrip("+").isdigit()
        and int(_max_socket_result_env) > 0
        else None
    )


def app(**kwargs):
    """Launch the detached Controller actor in the NDIF namespace."""
    args = ControllerDeploymentArgs(**kwargs)

    # The actor runs in its own Ray worker (inheriting only the node's ambient
    # env), and connects Loki in __init__ to ship its logs. Propagate this
    # launcher's Loki config into the actor's runtime_env so it reaches the same
    # Loki regardless of the worker node's environment.
    from .....common.providers.loki import LokiProvider

    LokiProvider.from_env()

    controller_cls = _import_from_path(args.controller_import_path)

    # get_if_exists makes relaunch idempotent: if a detached Controller from a
    # previous run is still alive (e.g. the launcher container restarted), reuse
    # it instead of failing on the name collision.
    controller_cls.options(
        name="Controller",
        namespace="NDIF",
        lifetime="detached",
        get_if_exists=True,
        runtime_env={
            "env_vars": {
                **LokiProvider.to_env(),
                # Forwarded on to each model actor by `cluster.deployment`; the
                # controller worker only inherits the node's ambient env, so it
                # has to be carried in explicitly like the provider config.
                **(
                    {
                        "NDIF_MAX_SOCKET_RESULT_BYTES": str(
                            args.max_socket_result_bytes
                        )
                    }
                    if args.max_socket_result_bytes
                    else {}
                ),
            }
        },
    ).remote(
        # Every field here becomes a constructor kwarg for the actor, and Ray
        # validates the signature — so a setting the actor does not take has to
        # be left out. `max_socket_result_bytes` is one: it reaches the model
        # actors through the environment above, not through the controller's
        # constructor.
        **args.model_dump(exclude={"max_socket_result_bytes"})
    )


if __name__ == "__main__":
    # Driver-side launch: attach to the running head and schedule the actor.
    # The detached actor persists after this process exits.
    ray.init(address="auto", namespace="NDIF", logging_level="error")
    app()
