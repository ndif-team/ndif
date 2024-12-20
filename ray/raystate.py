from typing import Any, Dict, List

try:
    from slugify import slugify
except:
    pass
import ray
import yaml
from pydantic import BaseModel
from ray.dashboard.modules.serve.sdk import ServeSubmissionClient
from ray.serve.schema import (
    DeploymentSchema,
    RayActorOptionsSchema,
    ServeApplicationSchema,
    ServeDeploySchema,
)

from .deployments.base import BaseDeploymentArgs, BaseModelDeploymentArgs


class ServiceConfigurationSchema(BaseModel):
    class ModelConfigurationSchema(BaseModel):

        model_import_path: str = None

        ray_actor_options: Dict[str, Any] = {}
        args: Dict[str, Any] = {}

        model_key: str
        num_replicas: int

    default_model_import_path: str
    request_import_path: str
    request_num_replicas: int

    models: List[ModelConfigurationSchema]


class RayState:

    def __init__(
        self,
        ray_config_path: str,
        service_config_path: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
        api_url: str,
    ) -> None:

        self.ray_config_path = ray_config_path
        self.service_config_path = service_config_path
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key
        self.api_url = api_url

        self.runtime_context = ray.get_runtime_context()
        self.ray_dashboard_url = (
            f"http://{self.runtime_context.worker.node.address_info['webui_url']}"
        )

        self.name_to_application: Dict[str, ServeApplicationSchema] = {}

    def load_from_disk(self):

        with open(self.ray_config_path, "r") as file:
            self.ray_config = ServeDeploySchema(**yaml.safe_load(file))

        with open(self.service_config_path, "r") as file:
            self.service_config = ServiceConfigurationSchema(**yaml.safe_load(file))

    def redeploy(self):

        self.load_from_disk()

        self.add_request_app()

        for model_config in self.service_config.models:
            self.add_model_app(model_config)

        self.apply()

    def apply(self) -> None:

        ServeSubmissionClient(self.ray_dashboard_url).deploy_applications(
            self.ray_config.dict(exclude_unset=True),
        )

    def add(self, application: ServeApplicationSchema):

        self.ray_config.applications.append(application)
        self.name_to_application[application.name] = application

    def add_request_app(self) -> None:
        application = ServeApplicationSchema(
            name="Request",
            import_path=self.service_config.request_import_path,
            route_prefix="/request",
            deployments=[
                DeploymentSchema(
                    name="RequestDeployment",
                    num_replicas=self.service_config.request_num_replicas,
                    ray_actor_options=RayActorOptionsSchema(
                        num_cpus=1, resources={"head": 1}
                    ),
                )
            ],
            args=BaseDeploymentArgs(
                api_url=self.api_url,
                object_store_url=self.object_store_url,
                object_store_access_key=self.object_store_access_key,
                object_store_secret_key=self.object_store_secret_key,
            ).model_dump(),
        )

        self.add(application)

    def add_model_app(
        self, model_config: ServiceConfigurationSchema.ModelConfigurationSchema
    ) -> None:

        model_key = slugify(model_config.model_key)

        model_config.args["model_key"] = model_config.model_key
        model_config.args["api_url"] = self.api_url
        model_config.args["object_store_url"] = self.object_store_url
        model_config.args["object_store_access_key"] = self.object_store_access_key
        model_config.args["object_store_secret_key"] = self.object_store_secret_key

        application = ServeApplicationSchema(
            name=f"Model:{model_key}",
            import_path=model_config.model_import_path
            or self.service_config.default_model_import_path,
            route_prefix=f"/Model:{model_key}",
            deployments=[
                DeploymentSchema(
                    name="ModelDeployment",
                    num_replicas=model_config.num_replicas,
                    ray_actor_options=model_config.ray_actor_options,
                )
            ],
            args=model_config.args,
            runtime_env={
                "env_vars": {"restart_hash": "", 
                             # For distributed model timeout handling
                             "TORCH_NCCL_ASYNC_ERROR_HANDLING": "0"}
            },
        )

        self.add(application)
