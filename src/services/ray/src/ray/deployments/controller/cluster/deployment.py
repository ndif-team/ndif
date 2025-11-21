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
        deployment_level: DeploymentLevel,
        gpus: list[int],
        size_bytes: int,
        dedicated: bool = False,
    ):
        self.model_key = model_key
        self.deployment_level = deployment_level
        self.gpus = gpus
        self.size_bytes = size_bytes
        self.dedicated = dedicated
        self.deployed = time.time()

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
            return actor.from_cache.remote(",".join(str(gpu) for gpu in self.gpus))
        except Exception:
            logger.exception(f"Error removing actor {self.model_key} from cache.")
            return None

    def create(self, node_name: str, deployment_args: BaseModelDeploymentArgs):
        try:
            actor = ModelActor.options(
                name=self.name,
                resources={f"node:{node_name}": 0.01},
                namespace="NDIF",
                lifetime="detached",
                runtime_env={
                    **SioProvider.to_env(),
                    **ObjectStoreProvider.to_env(),
                    **MailgunProvider.to_env(),
                },
            ).remote(**deployment_args.model_dump())

        except Exception:
            logger.exception(f"Error creating actor {self.model_key}.")
