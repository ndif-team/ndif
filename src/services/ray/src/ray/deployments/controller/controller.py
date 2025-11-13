import asyncio
import os
import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import ray
from pydantic import BaseModel
from ray import serve
from dataclasses import dataclass
from ray.serve import Application
from ray.util.state import list_actors
from ....types import MODEL_KEY
from ....logging.logger import set_logger
from ..modeling.base import BaseModelDeploymentArgs
from ..modeling.util import get_downloaded_models
from .cluster import Cluster, Deployment, DeploymentLevel


@dataclass
class DeploymentDelta:
    deployments_to_cache: List[Deployment]
    deployments_from_cache: List[Deployment]
    deployments_to_create: List[tuple[str, Deployment]]
    deployments_to_delete: List[Deployment]


class _ControllerDeployment:
    def __init__(
        self,
        deployments: List[MODEL_KEY],
        model_import_path: str,
        execution_timeout_seconds: float,
        model_cache_percentage: float,
        minimum_deployment_time_seconds: float,
    ):

        super().__init__()

        self.model_import_path = model_import_path
        self.execution_timeout_seconds = execution_timeout_seconds
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage
        self.runtime_context = ray.get_runtime_context()
        self.replica_context = serve.get_replica_context()
        self.logger = set_logger("Controller")
        
        self.state: dict[tuple[str, str], Deployment] = dict()

        self.cluster = Cluster(
            minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
            model_cache_percentage=self.model_cache_percentage,
        )
        
        self.cluster.update_nodes()
        
        if deployments and deployments != [""]:
            self._deploy(deployments, dedicated=True)
            
        asyncio.create_task(self.check_nodes())

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
    
    async def check_nodes(self):
        
        while True:
            self.cluster.update_nodes()
            await asyncio.sleep(int(os.environ.get("NDIF_CONTROLLER_SYNC_INTERVAL_S", "30")))
            
    def _deploy(self, model_keys: List[MODEL_KEY], dedicated: Optional[bool] = False):

        self.logger.info(f"Deploying models: {model_keys}, dedicated: {dedicated}")

        results, change = self.cluster.deploy(model_keys, dedicated=dedicated)

        if change:
            self.apply()

        return results

    async def deploy(self, model_keys: List[MODEL_KEY], dedicated: Optional[bool] = False):

       return self._deploy(model_keys, dedicated=dedicated)

    def build(self):

        new_state = {}

        deployments_to_cache = []
        deployments_from_cache = []
        deployments_to_create = []
        deployments_to_delete = []

        # For every node
        for id, node in self.cluster.nodes.items():
            # For every cached deployment
            for model_key, cached in node.cache.items():

                # It will always exist in the state if its now cached.
                existing_deployment = self.state.pop((id, model_key))

                # If the deployment is hot, we need to actually cache it.
                if existing_deployment.deployment_level == DeploymentLevel.HOT:
                    deployments_to_cache.append(cached)

                # Update state.
                new_state[(id, model_key)] = cached

            # For every deployed deployment
            for model_key, deployment in node.deployments.items():

                existing_deployment = self.state.pop((id, model_key), None)

                # If the deployment didn't exist before, we need to create it.
                if existing_deployment is None:
                    deployments_to_create.append((node.name, deployment))
                # If the deployment is warm, we need to move it from cache.
                elif existing_deployment.deployment_level == DeploymentLevel.WARM:
                    deployments_from_cache.append(deployment)
                # Update state.
                new_state[(id, model_key)] = deployment

        # For every deployment that doesn't exist in the new state, we need to delete it.
        for (id, model_key), deployment in self.state.items():
            deployments_to_delete.append(deployment)
            
        # Update state.
        self.state = new_state

        return DeploymentDelta(
            deployments_to_cache=deployments_to_cache,
            deployments_from_cache=deployments_from_cache,
            deployments_to_create=deployments_to_create,
            deployments_to_delete=deployments_to_delete,
        )

    def apply(self):

        self.logger.info(f"Applying state: {self.state}")

        deployment_delta = self.build()

        # Delete deployments
        for deployment in deployment_delta.deployments_to_delete:
            deployment.delete()

        cache_futures = []

        # Cache deployments
        for deployment in deployment_delta.deployments_to_cache:

            cache_future = deployment.cache()

            if cache_future is not None:
                cache_futures.append(cache_future)

        # Wait for cache operations to complete
        ray.get(cache_futures)

        # Deploy models from cache
        for deployment in deployment_delta.deployments_from_cache:
            deployment.from_cache()

        # Create models from disk
        for (name, deployment) in deployment_delta.deployments_to_create:
            deployment_args = BaseModelDeploymentArgs(
                model_key=deployment.model_key,
                cuda_devices=",".join(str(gpu) for gpu in deployment.gpus),
                execution_timeout=self.execution_timeout_seconds,
            )
            
            deployment.create(name, deployment_args)
            

    def get_deployment(self, model_key: MODEL_KEY) -> Optional[dict]:
        """Get the deployment of a model key (or None if not found)."""
        for node in self.cluster.nodes.values():
            if model_key in node.deployments.keys():
                return node.deployments[model_key].get_state()
        return None

    def status(self):

        ray_status = list_actors()

        status = {}

        for actor_state in ray_status:

            if actor_state.name.startswith("ModelActor:"):
                
                if actor_state.state in {"DEPENDENCIES_UNREADY", "PENDING_CREATION", "RESTARTING"}:
                    application_state = "DEPLOYING"
                elif actor_state.state == "ALIVE":
                    application_state = "RUNNING"
                elif actor_state.state == "DEAD":
                    application_state = "UNHEALTHY"
               
                status[actor_state.name] = {
                    "application_state": application_state,
                }

        existing_repo_ids = set()

        for node in self.cluster.nodes.values():

            for deployment in node.deployments.values():

                application_name = deployment.name

                status[application_name] = {
                    **status[application_name],
                    "deployment_level": deployment.deployment_level.name,
                    "dedicated": deployment.dedicated,
                    "model_key": deployment.model_key,
                    "repo_id": self.cluster.evaluator.cache[
                        deployment.model_key
                    ].config._name_or_path,
                    "revision": self.cluster.evaluator.cache[
                        deployment.model_key
                    ].revision,
                    "config": self.cluster.evaluator.cache[
                        deployment.model_key
                    ].config.to_json_string(),
                    "n_params": self.cluster.evaluator.cache[
                        deployment.model_key
                    ].n_params,
                }

                if (
                    not deployment.dedicated
                    and self.minimum_deployment_time_seconds is not None
                ):
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

            for cached_deployment in node.cache.values():

                application_name = cached_deployment.name

                status[application_name] = {
                    "deployment_level": DeploymentLevel.WARM.name,
                    "model_key": cached_deployment.model_key,
                    "repo_id": self.cluster.evaluator.cache[
                        cached_deployment.model_key
                    ].config._name_or_path,
                    "revision": self.cluster.evaluator.cache[
                        cached_deployment.model_key
                    ].revision,
                    "config": self.cluster.evaluator.cache[
                        cached_deployment.model_key
                    ].config.to_json_string(),
                    "n_params": self.cluster.evaluator.cache[
                        cached_deployment.model_key
                    ].n_params,
                }

                existing_repo_ids.add(
                    self.cluster.evaluator.cache[
                        cached_deployment.model_key
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
                                "gpus_required": len(deployment.gpus),
                            }
                            for model_key, deployment in node.deployments.items()
                        },
                    }
                    for node_id, node in self.cluster.nodes.items()
                }
            },
        }


@serve.deployment(ray_actor_options={"num_cpus": 1, "resources": {"head": 1}})
class ControllerDeployment(_ControllerDeployment):
    pass


class ControllerDeploymentArgs(BaseModel):

    deployments: List[MODEL_KEY] = os.environ.get("NDIF_DEPLOYMENTS", "").split("|")

    model_import_path: str = "src.ray.deployments.modeling.model:app"
    execution_timeout_seconds: Optional[float] = float(
        os.environ.get("NDIF_EXECUTION_TIMEOUT_SECONDS", "3600")
    )
    minimum_deployment_time_seconds: Optional[float] = float(
        os.environ.get("NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS", "3600")
    )
    model_cache_percentage: Optional[float] = float(
        os.environ.get("NDIF_MODEL_CACHE_PERCENTAGE", "0.9")
    )


def app(args: ControllerDeploymentArgs) -> Application:
    return ControllerDeployment.bind(**args.model_dump())
