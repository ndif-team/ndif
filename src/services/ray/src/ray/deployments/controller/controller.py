import os
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

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

from ....logging.logger import set_logger
from ....providers.objectstore import ObjectStoreProvider
from ....providers.socketio import SioProvider
from ....providers.mailgun import MailgunProvider
from ..modeling.base import BaseModelDeploymentArgs
from ..modeling.util import get_downloaded_models
from .cluster import Cluster, Deployment, DeploymentLevel


class _ControllerDeployment:
    def __init__(
        self,
        deployments: List[str],
        model_import_path: str,
        execution_timeout_seconds: float,
        model_cache_percentage: float,
        minimum_deployment_time_seconds: float = 60,
    ):

        super().__init__()

        self.model_import_path = model_import_path
        self.execution_timeout_seconds = execution_timeout_seconds
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage
        self.runtime_context = ray.get_runtime_context()
        self.replica_context = serve.get_replica_context()

        self.ray_dashboard_url = (
            f"http://{self.runtime_context.worker.node.address_info['webui_url']}"
        )

        self.logger = set_logger("Controller")

        self.client = ServeSubmissionClient(self.ray_dashboard_url)

        serve_details = ServeInstanceDetails(**self.client.get_serve_details())

        applications = []

        for application_details in serve_details.applications.values():

            application_schema = application_details.deployed_app_config

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

        if deployments:
            self.deploy(deployments, dedicated=True)

    def get_state(self, include_ray_state: bool = False) -> Dict[str, Any]:
        """Get the state of the controller."""

        state = {
            "cluster": self.cluster.get_state(include_ray_state=include_ray_state),
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "model_cache_percentage": self.model_cache_percentage,
            "minimum_deployment_time_seconds": self.minimum_deployment_time_seconds,
        }

        if include_ray_state:

            state["ray_dashboard_url"] = self.ray_dashboard_url
            state["runtime_context"] = self.runtime_context.get()
            state["replica_context"] = asdict(self.replica_context)
            state["serve_details"] = self.client.get_serve_details()
            

        state["datetime"] = datetime.now().isoformat()
        return state

    def deploy(self, model_keys: List[str], dedicated: Optional[bool] = False):

        self.logger.info(f"Deploying models: {model_keys}, dedicated: {dedicated}")

        results, change = self.cluster.deploy(model_keys, dedicated=dedicated)

        if change:
            self.apply()

        return results

    def deployment_to_application(
        self, deployment: Deployment, node_name: str
    ) -> ServeApplicationSchema:

        slugified_model_key = slugify(deployment.model_key)

        deployment_args = BaseModelDeploymentArgs(
            model_key=deployment.model_key,
            node_name=node_name,
            cached=deployment.cached,
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
                        runtime_env={
                            **SioProvider.to_env(),
                            **ObjectStoreProvider.to_env(),
                            **MailgunProvider.to_env(),
                        },
                    ),
                )
            ],
            args=deployment_args.model_dump(),
        )

    def build(self):

        self.state.applications = [self.state.applications[0]]

        for node in self.cluster.nodes.values():
            for deployment in node.deployments.values():

                self.state.applications.append(
                    self.deployment_to_application(
                        deployment,
                        node.name,
                    )
                )

    def apply(self):

        self.logger.info(f"Applying state: {self.state}")

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
                    "n_params": self.cluster.evaluator.cache[
                        deployment.model_key
                    ].n_params,
                }

                if self.minimum_deployment_time_seconds is not None:
                    status[application_name]["schedule"] = {
                        "end_time": deployment.end_time(
                            self.minimum_deployment_time_seconds
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
                        "n_params": self.cluster.evaluator.cache[
                            cached_model_key
                        ].n_params,
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

        return {
            "deployments": status,
            "cluster": {
                "nodes": {
                    node_id: {
                        "resources": {
                            "total_gpus": node.resources.total_gpus,
                            "gpu_memory_bytes": node.resources.gpu_memory_bytes,
                            "available_gpus": node.resources.available_gpus,
                        },
                        "deployments": {
                            model_key: {
                                "gpus_required": deployment.gpus_required,
                            }
                            for model_key, deployment in node.deployments.items()
                        },
                    }
                    for node_id, node in self.cluster.nodes.items()
                }
            },
        }

    def sync(self):
        """
        Sync the cluster with the latest state of the nodes. Used to update the cluster when a worker node connects or disconnects from Ray head.
        """

        self.cluster.update_nodes()

@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class ControllerDeployment(_ControllerDeployment):
    pass


class ControllerDeploymentArgs(BaseModel):

    deployments: List[str] = os.environ.get("NDIF_DEPLOYMENTS", "").split("|")

    model_import_path: str = "src.ray.deployments.modeling.model:app"
    execution_timeout_seconds: Optional[float] = None
    minimum_deployment_time_seconds: Optional[float] = None
    model_cache_percentage: Optional[float] = float(os.environ.get("NDIF_MODEL_CACHE_PERCENTAGE", "0.9"))


def app(args: ControllerDeploymentArgs) -> Application:
    return ControllerDeployment.bind(**args.model_dump())
