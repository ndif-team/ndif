import time
from enum import Enum

from ... import MODEL_KEY
import ray

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
    ):

        self.model_key = model_key
        self.deployment_level = deployment_level
        self.gpus_required = gpus_required
        self.size_bytes = size_bytes
        self.dedicated = dedicated
        
        self.deployed = time.time()
        
    def remove_from_cache(self):
        
        actor = ray.get_actor(f"ModelActor:{self.model_key}")
        ray.kill(actor)
