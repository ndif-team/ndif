import os
from typing import Dict

from pydantic import BaseModel
from ray import serve
from ray.serve import Application

from ..raystate import RayState


@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class ControllerDeployment:
    """
    A deployment class for controlling Ray serve configurations.

    This class manages the Ray state and provides functionality to deploy and redeploy
    serve configurations based on the provided configuration paths and object store details.
    """

    def __init__(
        self,
        ray_config_path: str,
        service_config_path: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
        api_url: str,
    ):
        """
        Initialize the ControllerDeployment.

        Args:
            ray_config_path (str): Path to the Ray configuration file.
            service_config_path (str): Path to the service configuration file.
            object_store_url (str): URL of the (minio) object store.
            object_store_access_key (str): Access key for the object store.
            object_store_secret_key (str): Secret key for the object store.
            api_url (str): URL of the API.
        """
        self.ray_config_path = ray_config_path
        self.service_config_path = service_config_path
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key

        self.api_url = api_url

        # Initialize the RayState object with the provided configuration
        # This is the core component used by Ray for managing the deployment state
        self.state = RayState(
            self.ray_config_path,
            self.service_config_path,
            self.object_store_url,
            self.object_store_access_key,
            self.object_store_secret_key,
            self.api_url,
        )

        # Perform initial deployment
        self.state.redeploy()
    def redeploy(self):
        """Redeploy serve configuration using service_config.yml"""

        self.state.redeploy()


class ControllerDeploymentArgs(BaseModel):
    """
    Arguments for the ControllerDeployment (represented as a Pydantic BaseModel).

    This class defines the arguments required for initializing the ControllerDeployment.
    """

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
    """
    Create and bind the ControllerDeployment with the provided arguments.

    Args:
        args (ControllerDeploymentArgs): Arguments for the ControllerDeployment.

    Returns:
        Application: The created and bound ControllerDeployment.
    """
    return ControllerDeployment.bind(**args.model_dump())
