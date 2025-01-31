import os
import uuid
from typing import Dict

from pydantic import BaseModel
from ray import serve
from ray.serve import Application

from ..raystate import RayState


@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class ControllerDeployment:
    def __init__(
        self,
        ray_config_path: str,
        service_config_path: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
        api_url: str,
    ):
        self.ray_config_path = ray_config_path
        self.service_config_path = service_config_path
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key

        self.api_url = api_url

        self.state = RayState(
            self.ray_config_path,
            self.service_config_path,
            self.object_store_url,
            self.object_store_access_key,
            self.object_store_secret_key,
            self.api_url,
        )

        self.state.redeploy()

    async def redeploy(self):
        """Redeploy serve configuration using service_config.yml"""

        self.state.redeploy()

    async def restart(self, name: str):
        
        self.state.name_to_application[name].runtime_env["env_vars"]["restart_hash"] = (
            str(uuid.uuid4())
        )

        self.state.apply()


class ControllerDeploymentArgs(BaseModel):

    ray_config_path: str = os.environ.get("RAY_CONFIG_PATH", None)
    service_config_path: str = os.environ.get("SERVICE_CONFIG_PATH", None)
    object_store_url: str = os.environ.get("OBJECT_STORE_URL", None)
    object_store_access_key: str = os.environ.get(
        "OBJECT_STORE_ACCESS_KEY", "minioadmin"
    )
    object_store_secret_key: str = os.environ.get(
        "OBJECT_STORE_SECRET_KEY", "minioadmin"
    )
    api_url: str = os.environ.get("API_URL", None)


def app(args: ControllerDeploymentArgs) -> Application:
    return ControllerDeployment.bind(**args.model_dump())