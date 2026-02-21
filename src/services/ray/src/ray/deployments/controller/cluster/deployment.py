import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict

import ray

from .....providers.mailgun import MailgunProvider
from .....providers.objectstore import ObjectStoreProvider
from .....providers.socketio import SioProvider
from .....types import MODEL_KEY
from ...modeling.base import ModelActor
from .....schema import DeploymentConfig

logger = logging.getLogger("ndif")


class DeploymentLevel(Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class Deployment:
    def __init__(
        self,
        deployment_level: DeploymentLevel,
        model_key: MODEL_KEY,
        gpus: list[int],
        size_bytes: int,
        deployment_cfg: DeploymentConfig | None = None,
        node_id: str | None = None,
    ):
        # Instance metadata
        self.deployed = time.time() # NOTE(cadentj): I wonder if this should be in create() instead?
        self.deployment_level = deployment_level

        self.model_key = model_key
        self.node_id = node_id # NOTE(cadentj): what is this used for?

        # These parameters are passed to the ModelActor but derived at runtime
        # so they are not included in the DeploymentConfig
        self.gpus = gpus
        self.size_bytes = size_bytes

        # The deployment config is None after eviction
        self.deployment_cfg = deployment_cfg

    @property
    def cpu_only(self) -> bool:
        if self.deployment_cfg is None:
            return False
        return self.deployment_cfg.device_map == "cpu"

    @property
    def dedicated(self) -> bool:
        if self.deployment_cfg is None:
            return False
        return self.deployment_cfg.dedicated

    @property
    def name(self):
        return f"ModelActor:{self.model_key}"

    @property
    def actor(self):
        return ray.get_actor(self.name, namespace="NDIF")

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the deployment."""

        return {
            "model_key": self.model_key,
            "deployment_level": self.deployment_level.value,
            "gpus": self.gpus,
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

    def create(self, node_name: str):
        if self.deployment_cfg is None:
            raise ValueError("Deployment config is required")
            
        try:
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

            ModelActor.options(
                name=self.name,
                resources={f"node:{node_name}": 0.01},
                namespace="NDIF",
                lifetime="detached",
                runtime_env={
                    "env_vars": env_vars,
                },
            ).remote(
                target_gpus=self.gpus,
                **self.deployment_cfg.model_dump()
            )

        except Exception:
            logger.exception(f"Error creating actor {self.model_key}.")
