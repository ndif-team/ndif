import os
import time
from typing import List, Optional

import ray
from pydantic import BaseModel
from ray import serve
from ray.dashboard.modules.serve.sdk import ServeSubmissionClient
from ray.serve import Application
from ray.serve.schema import (
    DeploymentSchema,
    RayActorOptionsSchema,
    ServeApplicationSchema,
    ServeDeploySchema,
    ServeInstanceDetails,
)
from slugify import slugify

from ....logging.logger import load_logger
from .. import MODEL_KEY
from ..modeling.base import BaseModelDeploymentArgs
from .cluster import Cluster, Deployment, DeploymentLevel
from ..modeling.util import get_downloaded_models

LOGGER = load_logger("Controller")


class _ControllerDeployment:
    def __init__(
        self,
        deployments: List[str],
        model_import_path: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
        api_url: str,
        execution_timeout_seconds: float,
        minimum_deployment_time_seconds: float,
        model_cache_percentage: float,
    ):

        super().__init__()

        self.model_import_path = model_import_path
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key
        self.api_url = api_url
        self.execution_timeout_seconds = execution_timeout_seconds
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage
        self.runtime_context = ray.get_runtime_context()
        self.replica_context = serve.get_replica_context()

        self.ray_dashboard_url = (
            f"http://{self.runtime_context.worker.node.address_info['webui_url']}"
        )

        self.client = ServeSubmissionClient(self.ray_dashboard_url)

        serve_details = ServeInstanceDetails(**self.client.get_serve_details())

        applications = []

        for application_details in serve_details.applications.values():

            application_schema = application_details.deployed_app_config
            # application_schema.deployments = [deployment.deployment_config for deployment in application_details.deployments.values()]

            applications.append(application_schema)

        self.state = ServeDeploySchema(
            applications=applications,
            proxy_location=serve_details.proxy_location,
            http_options=serve_details.http_options,
            grpc_options=serve_details.grpc_options,
            target_capacity=serve_details.target_capacity,
        )

        self.controller_application = list(serve_details.applications.values())[0]

        self.cluster = Cluster(
            minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
            model_cache_percentage=self.model_cache_percentage,
        )

        # self.deploy(self.deployments, dedicated=True)

    def deploy(self, model_keys: List[str], dedicated: Optional[bool] = False):

        LOGGER.info(f"Deploying models: {model_keys}, dedicated: {dedicated}")

        results, change = self.cluster.deploy(model_keys, dedicated=dedicated)

        if change:
            self.apply()

        return results

    def deployment_to_application(
        self, deployment: Deployment, node_name: str, cached: bool = False
    ) -> ServeApplicationSchema:

        slugified_model_key = slugify(deployment.model_key)

        deployment_args = BaseModelDeploymentArgs(
            model_key=deployment.model_key,
            node_name=node_name,
            cached=cached,
            api_url=self.api_url,
            object_store_url=self.object_store_url,
            object_store_access_key=self.object_store_access_key,
            object_store_secret_key=self.object_store_secret_key,
            execution_timeout=self.execution_timeout_seconds,
        )

        return ServeApplicationSchema(
            name=f"Model:{slugified_model_key}",
            import_path=self.model_import_path,
            route_prefix=f"/Model:{slugified_model_key}",
            deployments=[
                DeploymentSchema(
                    name="ModelDeployment",
                    num_replicas=1,
                    ray_actor_options=RayActorOptionsSchema(
                        num_cpus=1,
                        num_gpus=deployment.gpus_required,
                        resources={f"node:{node_name}": 0.01},
                    ),
                )
            ],
            args=deployment_args.model_dump(),
        )

    def build(self):

        self.state.applications = [self.state.applications[0]]

        for node in self.cluster.nodes.values():
            for deployment in node.deployments.values():

                cached = deployment.model_key in node.cache

                self.state.applications.append(
                    self.deployment_to_application(
                        deployment,
                        node.name,
                        cached=cached,
                    )
                )

    def apply(self):

        LOGGER.info(f"Applying state: {self.state}")

        self.build()

        self.client.deploy_applications(self.state.dict(exclude_unset=True))

    def status(self):

        serve_status = serve.status()

        status = {}

        for application_name, application in serve_status.applications.items():

            if application_name.startswith("Model"):

                status[application_name] = {
                    "application_state": application.status.value,
                }

        existing_repo_ids = set()

        for node in self.cluster.nodes.values():

            for deployment in node.deployments.values():

                application_name = f"Model:{slugify(deployment.model_key)}"

                status[application_name] = {
                    **status[application_name],
                    "deployment_level": deployment.deployment_level.name,
                    "dedicated": deployment.dedicated,
                    "model_key": deployment.model_key,
                    "repo_id": self.cluster.evaluator.cache[
                        deployment.model_key
                    ].config._name_or_path,
                    "config": self.cluster.evaluator.cache[
                        deployment.model_key
                    ].config.to_json_string(),
                    "minutesleft": (
                        (
                            self.minimum_deployment_time_seconds
                            - (time.time() - deployment.deployed)
                        )
                        // 60
                        if self.minimum_deployment_time_seconds is not None
                        else None
                    ),
                }

                existing_repo_ids.add(
                    self.cluster.evaluator.cache[
                        deployment.model_key
                    ].config._name_or_path
                )

            for cached_model_key in node.cache.keys():

                application_name = f"Model:{slugify(cached_model_key)}"

                if application_name not in status:

                    status[application_name] = {
                        "deployment_level": DeploymentLevel.WARM.name,
                        "model_key": cached_model_key,
                        "repo_id": self.cluster.evaluator.cache[
                            cached_model_key
                        ].config._name_or_path,
                        "config": self.cluster.evaluator.cache[
                            cached_model_key
                        ].config.to_json_string(),
                    }

                    existing_repo_ids.add(
                        self.cluster.evaluator.cache[
                            cached_model_key
                        ].config._name_or_path
                    )

        downloaded_models = get_downloaded_models()

        for repo_id in downloaded_models:

            if repo_id not in existing_repo_ids:

                status[repo_id] = {
                    "deployment_level": DeploymentLevel.COLD.name,
                    "repo_id": repo_id,
                }

        return {"deployments": status}


@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class ControllerDeployment(_ControllerDeployment):
    pass


class ControllerDeploymentArgs(BaseModel):

    deployments: List[str] = os.environ.get("NDIF_DEPLOYMENTS", "").split(",")

    object_store_url: str = os.environ.get("OBJECT_STORE_URL", None)
    object_store_access_key: str = os.environ.get(
        "OBJECT_STORE_ACCESS_KEY", "minioadmin"
    )
    object_store_secret_key: str = os.environ.get(
        "OBJECT_STORE_SECRET_KEY", "minioadmin"
    )
    api_url: str = os.environ.get("API_URL", None)
    model_import_path: str = "src.ray.deployments.modeling.model:app"
    execution_timeout_seconds: Optional[float] = None
    minimum_deployment_time_seconds: Optional[float] = None
    model_cache_percentage: Optional[float] = 0.5


def app(args: ControllerDeploymentArgs) -> Application:
    return ControllerDeployment.bind(**args.model_dump())
