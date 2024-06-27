import os
from typing import Dict

from pydantic import BaseModel
from ray import serve

from ..raystate import RayState

from ray.serve import Application


@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class ControllerDeployment:
    def __init__(
        self,
        ray_config_path: str,
        service_config_path: str,
        ray_dashboard_url: str,
        database_url: str,
        api_url: str,
    ):
        self.ray_config_path = ray_config_path
        self.service_config_path = service_config_path
        self.ray_dashboard_url = ray_dashboard_url
        self.database_url = database_url
        self.api_url = api_url

        self.state = RayState(
            self.ray_config_path,
            self.service_config_path,
            self.ray_dashboard_url,
            self.database_url,
            self.api_url,
        )
        
        self.state.apply()
        
class ControllerDeploymentArgs(BaseModel):

    ray_config_path: str = os.environ.get('RAY_CONFIG_PATH', None)
    service_config_path: str = os.environ.get('SERVICE_CONFIG_PATH', None)
    ray_dashboard_url: str = os.environ.get('RAY_DASHBOARD_URL', None)
    database_url: str = os.environ.get('DATABASE_URL', None)
    api_url: str = os.environ.get('API_URL', None)


def app(args: ControllerDeploymentArgs) -> Application:
    return ControllerDeployment.bind(**args.model_dump())
