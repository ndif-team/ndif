"""
Ray Serve controller for managing distributed model deployments.

This module defines the `ControllerDeployment`, which orchestrates
Serve applications across nodes in a Ray cluster. It translates
high-level `Deployment` specifications into Ray Serve applications,
applies updated deployment schemas, and monitors cluster state.

Key components:
    - `_ControllerDeployment`: Core logic for deployment orchestration.
    - `ControllerDeployment`: Ray Serve deployment wrapping the controller.
    - `ControllerDeploymentArgs`: Configuration schema for controller setup.
    - `app()`: Entrypoint for Serve application creation.

Used by the Ray head node to coordinate model deployment,
cache management, and cluster synchronization.
"""

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
from ....types import MODEL_KEY, RAY_APP_NAME, NODE_ID
from ....logging.logger import set_logger
from ....providers.objectstore import ObjectStoreProvider
from ....providers.socketio import SioProvider
from ....providers.mailgun import MailgunProvider
from ..modeling.base import BaseModelDeploymentArgs
from ..modeling.util import get_downloaded_models
from .cluster import Cluster, Deployment, DeploymentLevel


class _ControllerDeployment:
    """Core controller responsible for coordinating Ray Serve deployments.

    Handles deploying, syncing, and monitoring Serve applications across
    the Ray cluster. Maintains internal cluster state, caches, and resource
    allocation per node.

    Attributes:
        model_import_path: Import path to the model application factory.
        execution_timeout_seconds: Max model execution time per request.
        minimum_deployment_time_seconds: Minimum duration before teardown.
        model_cache_percentage: Fraction of GPU memory reserved for cache.
        ray_dashboard_url: Ray dashboard URL for submission client.
        client: ServeSubmissionClient used to deploy applications.
        cluster: Cluster object managing deployment state and nodes.
    """
    
    def __init__(
        self,
        deployments: List[str],
        model_import_path: str,
        execution_timeout_seconds: float,
        model_cache_percentage: float,
        minimum_deployment_time_seconds: float = 60,
    ):
        """Initialize the controller with deployment configuration.

        Args:
            deployments: List of model keys to deploy.
            model_import_path: Import path for model deployment app.
            execution_timeout_seconds: Maximum execution timeout.
            model_cache_percentage: Percentage of cache memory to use.
            minimum_deployment_time_seconds: Minimum up-time per model.
        """
        super().__init__()

        self.model_import_path = model_import_path
        self.execution_timeout_seconds = execution_timeout_seconds
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage
        self.runtime_context = ray.get_runtime_context()
        self.replica_context = serve.get_replica_context()
        self.logger = set_logger("Controller")

        if os.getenv("RAY_DASHBOARD_URL"):
            self.ray_dashboard_url = os.getenv("RAY_DASHBOARD_URL")
        else:   
            self.logger.warning("RAY_DASHBOARD_URL is not set, using default dashboard URL")
            self.ray_dashboard_url = (
                f"http://{self.runtime_context.worker.node.address_info['webui_url']}"
            )


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

        if deployments and deployments != ['']:
            self.deploy(deployments, dedicated=True)

    def get_state(self, include_ray_state: bool = False) -> Dict[str, Any]:
        """Get the state of the controller."""
        """Return controller state and optionally Ray runtime info.

        Args:
            include_ray_state: Whether to include Ray runtime context.

        Returns:
            Dict[str, Any]: Controller state summary.
        """

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
        """Deploy one or more model keys to the cluster.

        Args:
            model_keys: Model identifiers to deploy.
            dedicated: Whether to assign each deployment to a dedicated node.

        Returns:
            Tuple[List[Any], bool]: Deployment results and change flag.

        Example:
            >>> ctrl.deploy(["meta-llama/Llama-3.1-8B"], dedicated=True)
        """
        self.logger.info(f"Deploying models: {model_keys}, dedicated: {dedicated}")

        results, change = self.cluster.deploy(model_keys, dedicated=dedicated)

        if change:
            self.apply()

        return results

    def deployment_to_application(
        self, deployment: Deployment, node_name: NODE_ID
    ) -> ServeApplicationSchema:
        """Convert a deployment object into a Serve application schema.

        Args:
            deployment: Deployment configuration object.
            node_name: Target node identifier.

        Returns:
            ServeApplicationSchema: Application schema ready for deployment.
        """
        deployment_args = BaseModelDeploymentArgs(
            model_key=deployment.model_key,
            node_name=node_name,
            cached=deployment.cached,
            execution_timeout=self.execution_timeout_seconds,
        )

        return ServeApplicationSchema(
            name=RAY_APP_NAME(deployment.model_key),
            import_path=self.model_import_path,
            route_prefix=f"/{RAY_APP_NAME(deployment.model_key)}",
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
        """Build a full ServeDeploySchema from current cluster state."""
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
        """Apply the current state to Ray Serve via ServeSubmissionClient."""
        self.logger.info(f"Applying state: {self.state}")

        self.build()

        self.client.deploy_applications(self.state.dict(exclude_unset=True))

    def get_deployment(self, model_key: MODEL_KEY) -> Optional[dict]:
        """Get the deployment of a model key (or None if not found)."""
        """Get deployment metadata for a specific model.

        Args:
            model_key: Model key to look up.

        Returns:
            Optional[dict]: Deployment state if found, otherwise None.
        """
        for node in self.cluster.nodes.values():
            if model_key in node.deployments.keys():
                return node.deployments[model_key].get_state()
        return None

    def status(self):
        """Return status of all deployments and node resources.

        Returns:
            Dict[str, Any]: Deployment and cluster-level status snapshot.
        """
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

                application_name = RAY_APP_NAME(deployment.model_key)

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

                application_name = RAY_APP_NAME(MODEL_KEY(cached_model_key))

                if application_name not in status:

                    status[application_name] = {
                        "deployment_level": DeploymentLevel.WARM.name,
                        "model_key": cached_model_key,
                        "repo_id": self.cluster.evaluator.cache[
                            cached_model_key
                        ].config._name_or_path,
                        "revision": self.cluster.evaluator.cache[
                            cached_model_key
                        ].revision,
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
    """Ray Serve deployment wrapper around `_ControllerDeployment`."""
    pass


class ControllerDeploymentArgs(BaseModel):
    """Pydantic model defining startup arguments for the controller."""
    deployments: List[str] = os.environ.get("NDIF_DEPLOYMENTS", "").split("|")

    model_import_path: str = "src.ray.deployments.modeling.model:app"
    execution_timeout_seconds: Optional[float] = None
    minimum_deployment_time_seconds: Optional[float] = None
    model_cache_percentage: Optional[float] = float(os.environ.get("NDIF_MODEL_CACHE_PERCENTAGE", "0.9"))


def app(args: ControllerDeploymentArgs) -> Application:
    """Entrypoint for the Ray Serve controller application.

    Args:
        args: Configuration object defining controller parameters.

    Returns:
        Application: Bound Ray Serve application ready for deployment.

    Example:
        >>> from ray.deployments.controller.controller import app, ControllerDeploymentArgs
        >>> args = ControllerDeploymentArgs(deployments=["meta-llama/Llama-3.1-8B"])
        >>> serve.run(app(args))
    """
    return ControllerDeployment.bind(**args.model_dump())
