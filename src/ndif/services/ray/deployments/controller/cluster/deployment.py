import importlib
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Union

import ray

from ......common.types import MODEL_KEY, REPLICA_ID
from ...modeling.base import BaseModelDeployment, BaseModelDeploymentArgs

logger = logging.getLogger("ndif.controller")


def _provider_runtime_env() -> Dict[str, str]:
    """Env vars the model actor needs to reach the shared services.

    The actor runs in its own Ray worker process and re-establishes its own
    provider connections at import: Redis (to publish responses back to the
    client's websocket channel via ``request.respond``), the object store (to
    upload results), and Loki/Influx (telemetry). Ray workers only inherit the
    node's ambient env, so propagate *this* controller process's provider config
    into the actor's ``runtime_env`` — the same wiring the original injected at
    actor creation — so the actor connects to the exact same services.

    Imported lazily so merely importing this module doesn't open provider
    connections in the controller process; ``from_env()`` re-reads the current
    environment before exporting.
    """
    from ......common.providers.influx import InfluxProvider
    from ......common.providers.loki import LokiProvider
    from ......common.providers.objectstore import ObjectStoreProvider
    from ......common.providers.redis import RedisProvider

    env: Dict[str, str] = {}
    for provider in (RedisProvider, ObjectStoreProvider, LokiProvider, InfluxProvider):
        provider.from_env()
        env.update(provider.to_env())
    return env


class DeploymentLevel(Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class Deployment:
    """One replica of a model: its placement bookkeeping plus the Ray actor ops
    (create / delete / cache / from_cache) the controller drives it through."""

    def __init__(
        self,
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
        deployment_level: DeploymentLevel,
        gpus: dict[int, int],
        size_bytes: int,
        pinned: bool = False,
        node_id: str = None,
        execution_timeout_seconds: float | None = None,
        actor_class: Optional[Union[str, type]] = None,
        dtype: Optional[str] = None,
        trusted: bool = False,
    ):
        self.model_key = model_key
        self.replica_id = replica_id
        self.deployment_level = deployment_level
        self.gpus = gpus
        self.size_bytes = size_bytes
        self.pinned = pinned
        self.node_id = node_id
        self.execution_timeout_seconds = execution_timeout_seconds
        self.actor_class = actor_class
        self.dtype = dtype
        # Whether the model loads with HF trust_remote_code (from the deploying
        # request's trusted flag); the controller passes it into the actor args.
        self.trusted = trusted
        self.deployed = time.time()

    def _resolve_actor_class(self) -> type[BaseModelDeployment]:
        """Resolve ``self.actor_class`` to a concrete Ray actor class.

        Strings are imported as dotted paths; class objects are returned as-is.
        The controller substitutes its configured default for any ``None`` at
        construction time, so ``None`` here is a programming error.
        """
        if self.actor_class is None:
            raise ValueError(
                "actor_class was not set on Deployment — the controller should "
                "have populated it from default_model_actor_class before create()."
            )
        if isinstance(self.actor_class, str):
            module_path, _, class_name = self.actor_class.rpartition(".")
            if not module_path:
                raise ValueError(
                    f"actor_class {self.actor_class!r} is not a dotted import path"
                )
            module = importlib.import_module(module_path)
            return getattr(module, class_name)
        return self.actor_class

    @property
    def name(self):
        return f"{self.replica_id}:ModelActor:{self.model_key}"

    @property
    def actor(self):
        return ray.get_actor(self.name, namespace="NDIF")

    def get_state(self) -> Dict[str, Any]:
        """Snapshot used to build ReplicaState and report status."""
        if self.actor_class is None:
            actor_class_repr = None
        elif isinstance(self.actor_class, str):
            actor_class_repr = self.actor_class
        else:
            actor_class_repr = (
                f"{self.actor_class.__module__}.{self.actor_class.__qualname__}"
            )

        return {
            "model_key": self.model_key,
            "replica_id": self.replica_id,
            "deployment_level": self.deployment_level.value,
            "gpus": self.gpus,
            "size_bytes": self.size_bytes,
            "pinned": self.pinned,
            "node_id": self.node_id,
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "actor_class": actor_class_repr,
            "deployed": self.deployed,
        }

    def end_time(self, minimum_deployment_time_seconds: int) -> datetime:
        return datetime.fromtimestamp(
            self.deployed + minimum_deployment_time_seconds, tz=timezone.utc
        )

    def delete(self):
        try:
            ray.kill(self.actor, no_restart=True)
        except Exception:
            logger.exception(f"Error deleting actor {self.model_key}.")

    def restart(self):
        try:
            ray.kill(self.actor, no_restart=False)
        except Exception:
            logger.exception(f"Error restarting actor {self.model_key}.")

    def cache(self):
        """Tell the actor to offload weights GPU->CPU; return the Ray future."""
        try:
            return self.actor.to_cache.remote()
        except Exception:
            logger.exception(f"Error adding actor {self.model_key} to cache.")
            return None

    def from_cache(self):
        """Tell the actor to restore weights CPU->GPU; return the Ray future."""
        try:
            return self.actor.from_cache.remote(self.gpus)
        except Exception:
            logger.exception(f"Error removing actor {self.model_key} from cache.")
            return None

    def create(self, node_name: str, deployment_args: BaseModelDeploymentArgs):
        try:
            # Inject the assigned GPU allocation so the actor targets those GPUs.
            deployment_args.gpu_mem_bytes_by_id = self.gpus

            env_vars = {
                # Keep Ray from setting CUDA_VISIBLE_DEVICES, so the actor sees
                # all GPUs; targeting is handled by max_memory in the actor.
                "RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES": "1",
                # Reduce CUDA memory fragmentation.
                "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
                # Redis / object store / Loki / Influx config so the actor's
                # providers connect to the same services this controller does.
                **_provider_runtime_env(),
                # Label this actor's telemetry as the model service. The
                # provider env above carries the *controller's* NDIF_SERVICE;
                # override it so a model actor's logs/metrics attribute to
                # "model", not the controller.
                "NDIF_SERVICE": "model",
            }

            actor_class = self._resolve_actor_class()

            actor_class.options(
                name=self.name,
                resources={f"node:{node_name}": 0.01},
                namespace="NDIF",
                lifetime="detached",
                runtime_env={"env_vars": env_vars},
            ).remote(**deployment_args.model_dump())

        except Exception:
            logger.exception(f"Error creating actor {self.model_key}.")
