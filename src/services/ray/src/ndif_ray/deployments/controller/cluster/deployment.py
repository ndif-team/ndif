import time
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict
import ray

#from .....types import MODEL_KEY
from ndif_shared.types import MODEL_KEY
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
        gpus_required: int,
        size_bytes: int,
        dedicated: bool = False,
        cached: bool = False,
    ):

        self.model_key = MODEL_KEY(model_key)
        self.deployment_level = deployment_level
        self.gpus_required = gpus_required
        self.size_bytes = size_bytes
        self.dedicated = dedicated
        self.cached = cached
        self.deployed = time.time()

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the deployment."""

        return {
            "model_key": self.model_key,
            "deployment_level": self.deployment_level.value,
            "gpus_required": self.gpus_required,
            "size_bytes": self.size_bytes,
            "dedicated": self.dedicated,
            "cached": self.cached,
            "deployed": self.deployed,
        }


    def end_time(self, minimim_deployment_time_seconds: int) -> datetime:

        return datetime.fromtimestamp(
            self.deployed + minimim_deployment_time_seconds, tz=timezone.utc
        )

    def delete(self):

        try:
            actor = ray.get_actor(f"ModelActor:{self.model_key}")
            ray.kill(actor)
        except Exception:
            logger.exception(f"Error removing actor {self.model_key} from cache.")
            pass
        
    def cache(self):
        
        try:
            actor = ray.get_actor(f"ModelActor:{self.model_key}")
            return actor.to_cache.remote()
        except Exception:
            logger.exception(f"Error adding actor {self.model_key} to cache.")
            pass
