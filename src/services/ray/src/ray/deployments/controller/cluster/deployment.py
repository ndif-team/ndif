import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict

import ray

from .....providers.mailgun import MailgunProvider
from .....providers.objectstore import ObjectStoreProvider
from .....providers.socketio import SioProvider
from .....types import MODEL_KEY, REPLICA_ID
from ...modeling.base import BaseModelDeploymentArgs, ModelActor

logger = logging.getLogger("ndif")


class DeploymentLevel(Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class Deployment:
    def __init__(
        self,
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
        deployment_level: DeploymentLevel,
        gpu_mem_bytes_by_id: Dict[int, int],
        gpu_memory_fraction: float | None,
        size_bytes: int,
        dedicated: bool = False,
        node_id: str = None,
    ):
        self.model_key = model_key
        self.replica_id = replica_id
        self.deployment_level = deployment_level
        self.gpu_mem_bytes_by_id = gpu_mem_bytes_by_id
        self.gpu_memory_fraction = gpu_memory_fraction
        self.size_bytes = size_bytes
        self.dedicated = dedicated
        self.node_id = node_id
        self.deployed = time.time()

    @property
    def name(self):
        return f"ModelActor:{self.model_key}:{self.replica_id}"

    @property
    def actor(self):
        return ray.get_actor(self.name, namespace="NDIF")
    
    @property
    def gpus(self):
        return list(self.gpu_mem_bytes_by_id.keys())

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the deployment."""

        return {
            "model_key": self.model_key,
            "replica_id": self.replica_id,
            "deployment_level": self.deployment_level.value,
            "gpu_mem_bytes_by_id": self.gpu_mem_bytes_by_id,
            "gpu_memory_fraction": self.gpu_memory_fraction,
            "size_bytes": self.size_bytes,
            "dedicated": self.dedicated,
            "node_id": self.node_id,
            "deployed": self.deployed,
        }

    def end_time(self, minimim_deployment_time_seconds: int) -> datetime:
        return datetime.fromtimestamp(
            self.deployed + minimim_deployment_time_seconds, tz=timezone.utc
        )

    def delete(self):
        try:
            actor = self.actor
            ray.kill(actor, no_restart=True)
        except Exception:
            logger.exception(f"Error deleting actor {self.model_key}.")
            pass

    def restart(self):
        try:
            actor = self.actor
            ray.kill(actor, no_restart=False)
        except Exception:
            logger.exception(f"Error restarting actor {self.model_key}.")
            pass

    def cache(self):
        try:
            actor = self.actor
            return actor.to_cache.remote()
        except Exception:
            logger.exception(f"Error adding actor {self.model_key} to cache.")
            return None

    def from_cache(self):
        try:
            actor = self.actor
            return actor.from_cache.remote(self.gpus)
        except Exception:
            logger.exception(f"Error removing actor {self.model_key} from cache.")
            return None

    def create(self, node_name: str, deployment_args: BaseModelDeploymentArgs):
        try:
            # Inject the assigned GPU indices so the actor knows which GPUs to target
            deployment_args.target_gpus = self.gpus

            env_vars = {
                # Prevent Ray from setting CUDA_VISIBLE_DEVICES, so the actor
                # inherits full GPU visibility from the worker node. GPU targeting
                # is handled by max_memory in the actor's load_from_disk/from_cache.
                "RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES": "1",
                **SioProvider.to_env(),
                **ObjectStoreProvider.to_env(),
                **MailgunProvider.to_env(),
            }

            env_vars = {k: v for k, v in env_vars.items() if v is not None}

            actor = ModelActor.options(
                name=self.name,
                resources={f"node:{node_name}": 0.01},
                namespace="NDIF",
                lifetime="detached",
                runtime_env={
                    "env_vars": env_vars,
                },
            ).remote(**deployment_args.model_dump())

        except Exception:
            logger.exception(f"Error creating actor {self.model_key}.")
