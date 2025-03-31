import os
import uuid
from typing import Dict, List, Union

from pydantic import BaseModel
from ray import serve
from ray.serve import Application

from ..raystate import RayState
from .scheduler import SchedulingActor
from ..deployments.model import BaseModelDeploymentArgs 
from ..schema import ModelConfigurationSchema
class BaseControllerDeployment:
    def __init__(
        self,
        ray_config_path: str,
        service_config_path: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
        api_url: str,
    ):
        self.replica_context = serve.get_replica_context()
        
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
        self.model_configurations = {}

    async def redeploy(self):
        """Redeploy serve configuration using service_config.yml"""
        self.state.redeploy()

    async def restart(self, name: str):
        self.state.name_to_application[name].runtime_env["env_vars"]["restart_hash"] = (
            str(uuid.uuid4())
        )
        self.state.apply()
        
    async def set_model_configuration(self, name:str, configuration: Dict):
        self.model_configurations[name] = configuration
        
    async def get_model_configurations(self):
        return self.model_configurations

@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class ControllerDeployment(BaseControllerDeployment):
    pass



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

@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class SchedulingControllerDeployment(BaseControllerDeployment):
    def __init__(
        self,
        google_creds_path: str,
        google_calendar_id: str,
        check_interval_s: float,
        **kwargs
    ):
        # Initialize the base controller first
        super().__init__(
            **kwargs
        )
        
        self.google_creds_path = google_creds_path
        self.google_calendar_id = google_calendar_id
        self.check_interval_s = check_interval_s
        
        # Create a handle to this deployment for the scheduler to use
        handle = self.replica_context.deployment_handle
        
        # Initialize the scheduler actor
        self.scheduler = SchedulingActor.remote(
            google_credentials_path=self.google_creds_path,
            google_calendar_id=self.google_calendar_id,
            check_interval=self.check_interval_s,
            controller_handle=handle,
        )
        
        # Start the scheduler
        self.scheduler.start.remote()
        
    async def deploy(self, model_keys: Union[List[str], str]):
        """
        Deploy models based on the provided model keys.
        
        Args:
            model_keys: List of model keys to be deployed
        """
        
        if isinstance(model_keys, str):
            model_keys = [model_keys]
        
        print(f"Deploying models from scheduling actor: {model_keys}")
        
        # First, reset the state to clear existing model deployments
        self.state.reset()
        
        # Create a dictionary of existing model configurations by model key for quick lookup
        existing_configs = {
            config.args.model_key: config
            for config in self.state.service_config.models
            if hasattr(config.args, 'model_key')
        }
        
        # Create and add model configurations for each model key
        for model_key in model_keys:
            # Check if we have an existing configuration for this model
            if model_key in existing_configs:
                # Use the existing configuration
                model_config = existing_configs[model_key]
                print(f"Using existing configuration for model: {model_key}")
            else:
       
                # Create the model configuration
                model_config = ModelConfigurationSchema(
                    args=BaseModelDeploymentArgs(
                        model_key=model_key,
                    ),
                    num_replicas=1,  # Default to 1 replica
                    ray_actor_options={"num_cpus": 1, "num_gpus": 1}  # Default actor options
                )
                print(f"Created new configuration for model: {model_key}")
            
            # Add the model to the state
            self.state.add_model_app(model_config)
        
        # Apply the changes to update the deployments
        self.state.apply()
        

class SchedulingControllerDeploymentArgs(ControllerDeploymentArgs):
    google_creds_path: str = os.environ.get("SCHEDULING_GOOGLE_CREDS_PATH", None)
    google_calendar_id: str = os.environ.get("SCHEDULING_GOOGLE_CALENDAR_ID", None)
    check_interval_s: float = float(os.environ.get("SCHEDULING_CHECK_INTERVAL_S", "60"))


def scheduling_app(args: SchedulingControllerDeploymentArgs) -> Application:
    return SchedulingControllerDeployment.bind(**args.model_dump())
