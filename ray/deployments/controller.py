import os
from typing import Dict

import ray
from pydantic import BaseModel
from ray import serve
from ray.serve.handle import DeploymentHandle, DeploymentResponse

from ..raystate import RayState
class ControllerDeploymentArgs(BaseModel):

    ray_config_path: str
    model_config_path: str


@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class ControllerDeployment:
    def __init__(self, ray_config_path: str, model_config_path: str):
        self.ray_config_path = ray_config_path
        self.model_config_path = model_config_path
        
        
        self.state = RayState(ray_config_path)

    async def __call__(self):
        return os.getpid()

