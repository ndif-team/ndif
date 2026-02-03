import asyncio
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from importlib.metadata import distributions, packages_distributions
from typing import Any, Dict, List, Optional

import ray
from pydantic import BaseModel
from ray.util.state import list_actors

from ....logging.logger import set_logger
from ....providers.mailgun import MailgunProvider
from ....providers.objectstore import ObjectStoreProvider
from ....providers.socketio import SioProvider
from ....types import MODEL_KEY
from ..modeling.base import BaseModelDeploymentArgs
from ..modeling.util import get_downloaded_models
from .cluster import Cluster, Deployment, DeploymentLevel


@dataclass
class DeploymentDelta:
    deployments_to_cache: List[Deployment]
    deployments_from_cache: List[Deployment]
    deployments_to_create: List[tuple[str, Deployment]]
    deployments_to_delete: List[Deployment]


class _ControllerActor:
    def __init__(
        self,
        deployments: List[MODEL_KEY],
        model_import_path: str,
        execution_timeout_seconds: float,
        model_cache_percentage: float,
        minimum_deployment_time_seconds: float,
        replica_count: Optional[int] = None,
    ):
        super().__init__()

        self.model_import_path = model_import_path
        self.execution_timeout_seconds = execution_timeout_seconds
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.model_cache_percentage = model_cache_percentage
        self.runtime_context = ray.get_runtime_context()
        self.logger = set_logger("Controller")
        self.replica_count = replica_count
        self.desired_replicas: Dict[MODEL_KEY, int] = {}

        self.state: dict[tuple[str, str, int], Deployment] = dict()

        self.cluster = Cluster(
            minimum_deployment_time_seconds=self.minimum_deployment_time_seconds,
            model_cache_percentage=self.model_cache_percentage,
        )

        if deployments and deployments != [""]:
            self._deploy(deployments, dedicated=True)

        self.cluster.update_nodes()

        asyncio.create_task(self.check_nodes())

    def get_state(self, include_ray_state: bool = False) -> Dict[str, Any]:
        """Get the state of the controller."""

        state = {
            "cluster": self.cluster.get_state(include_ray_state=include_ray_state),
            "execution_timeout_seconds": self.execution_timeout_seconds,
            "model_cache_percentage": self.model_cache_percentage,
            "minimum_deployment_time_seconds": self.minimum_deployment_time_seconds,
            "replica_count": self.replica_count,
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
            await asyncio.sleep(
                int(os.environ.get("NDIF_CONTROLLER_SYNC_INTERVAL_S", "30"))
            )

    def _deploy(self, model_keys: List[MODEL_KEY], dedicated: Optional[bool] = False, replicas: Optional[int] = None):
        self.logger.info(f"Deploying models: {model_keys}, dedicated: {dedicated}")

        if replicas is None:
            # default to 1
            replicas = 1
        for model_key in model_keys:
            self.desired_replicas[model_key] = replicas

        results, change = self.cluster.deploy(model_keys, dedicated=dedicated, replicas=replicas)

        if change:
            self.apply()

        return results

    async def deploy(
        self, model_keys: List[MODEL_KEY], replicas: Optional[int] = None, dedicated: Optional[bool] = False, 
    ):
        self.logger.info(f"Deploying models: {model_keys}, dedicated: {dedicated}, replicas: {replicas}")
        return self._deploy(model_keys, dedicated=dedicated, replicas=replicas)

    def evict(
        self,
        model_keys: List[MODEL_KEY],
        replica_keys: Optional[List[tuple[MODEL_KEY, int]]] = None,
        cache: bool = True,
    ):
        """Evict models from the cluster."""
        if replica_keys:
            evicted_by_model: Dict[MODEL_KEY, int] = {}
            for model_key, _replica_id in replica_keys:
                evicted_by_model[model_key] = evicted_by_model.get(model_key, 0) + 1
            for model_key, count in evicted_by_model.items():
                current_desired = self.desired_replicas.get(
                    model_key, self._current_replica_count(model_key)
                )
                self.desired_replicas[model_key] = max(0, current_desired - count)
        else:
            for model_key in model_keys:
                self.desired_replicas[model_key] = 0

        results, change = self.cluster.evict(
            model_keys, replica_keys=replica_keys, cache=cache
        )

        if change:
            self.apply()

        return results

    def _current_replica_count(self, model_key: MODEL_KEY) -> int:
        existing_replica_ids = set()
        for node in self.cluster.nodes.values():
            for (deployment_model_key, deployment_replica_id) in node.deployments.keys():
                if deployment_model_key == model_key:
                    existing_replica_ids.add(deployment_replica_id)
        return len(existing_replica_ids)

    def scale(self, model_key: MODEL_KEY, replicas: int, dedicated: Optional[bool] = False):
        """Scale a model up to a replica count.

        This only adds missing replicas up to replicas-1. It does not evict or
        down-scale existing replicas.
        """
        if replicas <= 0:
            raise ValueError("replicas must be a positive integer")

        current_replica_count = self._current_replica_count(model_key)
        if current_replica_count >= replicas:
            return {
                "deploy": {"result": {}, "evictions": set()},
                "current_replicas": current_replica_count,
                "target_replicas": replicas,
                "changed": False,
            }

        self.desired_replicas[model_key] = replicas
        deploy_results, deploy_change = self.cluster.deploy(
            [model_key], dedicated=dedicated, replicas=replicas
        )

        if deploy_change:
            self.apply()

        return {
            "deploy": deploy_results,
            "current_replicas": current_replica_count,
            "target_replicas": replicas,
            "changed": deploy_change,
        }

    def scale_up(self, model_key: MODEL_KEY, replicas: int, dedicated: Optional[bool] = False):
        """Add a number of replicas to an existing deployment."""
        if replicas <= 0:
            raise ValueError("replicas must be a positive integer")

        current_replica_count = self._current_replica_count(model_key)
        target_replicas = current_replica_count + replicas

        self.desired_replicas[model_key] = target_replicas
        deploy_results, deploy_change = self.cluster.deploy(
            [model_key], dedicated=dedicated, replicas=target_replicas
        )

        if deploy_change:
            self.apply()

        return {
            "deploy": deploy_results,
            "current_replicas": current_replica_count,
            "target_replicas": target_replicas,
            "changed": deploy_change,
        }

    def build(self):
        new_state = {}

        deployments_to_cache = []
        deployments_from_cache = []
        deployments_to_create = []
        deployments_to_delete = []
        # For every node
        for id, node in self.cluster.nodes.items():
            # For every cached deployment
            for (model_key, replica_id), cached in node.cache.items():
                # It will always exist in the state if its now cached.
                existing_deployment = self.state.pop((id, cached.model_key, cached.replica_id))

                # If the deployment is hot, we need to actually cache it.
                if existing_deployment.deployment_level == DeploymentLevel.HOT:
                    deployments_to_cache.append(cached)

                # Update state.
                new_state[(id, cached.model_key, cached.replica_id)] = cached

            # For every deployed deployment
            for (model_key, replica_id), deployment in node.deployments.items():
                existing_deployment = self.state.pop((id, deployment.model_key, deployment.replica_id), None)

                # If the deployment didn't exist before, we need to create it.
                if existing_deployment is None:
                    deployments_to_create.append((node.name, deployment))
                # If the deployment is warm, we need to move it from cache.
                elif existing_deployment.deployment_level == DeploymentLevel.WARM:
                    deployments_from_cache.append(deployment)
                # Update state.
                new_state[(id, deployment.model_key, deployment.replica_id)] = deployment

        # For every deployment that doesn't exist in the new state, we need to delete it.
        for (id, model_key, replica_id), deployment in self.state.items():
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
        for name, deployment in deployment_delta.deployments_to_create:
            deployment_args = BaseModelDeploymentArgs(
                model_key=deployment.model_key,
                replica_id=deployment.replica_id,
                cuda_devices=",".join(str(gpu) for gpu in deployment.gpus),
                execution_timeout=self.execution_timeout_seconds,
                gpu_memory_fraction=deployment.gpu_memory_fraction,
            )

            deployment.create(name, deployment_args)

    def get_deployment(self, model_key: MODEL_KEY, replica_id: Optional[int] = None) -> Optional[dict]:
        """Get the deployment of a model key (or None if not found)."""
        for node in self.cluster.nodes.values():
            if replica_id is None:
                for (deployment_model_key, deployment_replica_id), deployment in node.deployments.items():
                    if deployment_model_key == model_key:
                        return deployment.get_state()
            else:
                if (model_key, replica_id) in node.deployments.keys():
                    return node.deployments[(model_key, replica_id)].get_state()
        return None

    def env(self) -> Dict[str, Any]:
        """Get the Python environment information.

        Returns:
            Dictionary containing Python version and installed pip packages.
        """
        pd_map = packages_distributions()
        dist_to_imports = {}
        for import_name, dist_names in pd_map.items():
            for dist_name in dist_names:
                if dist_name not in dist_to_imports:
                    dist_to_imports[dist_name] = []
                dist_to_imports[dist_name].append(import_name)

        packages = {}
        for dist in distributions():
            dist_name = dist.metadata["Name"]
            version = dist.version

            # Get import names from packages_distributions mapping
            import_names = dist_to_imports.get(dist_name, [])

            if import_names:
                for imp_name in import_names:
                    packages[imp_name] = version
            else:
                # Fallback to distribution name if no import mapping found
                packages[dist_name] = version

        return {
            "python_version": sys.version,
            "packages": packages,
        }

    def status(self):
        ray_status = list_actors()

        status = {}

        for actor_state in ray_status:
            if actor_state.name.startswith("ModelActor:"):
                if actor_state.state in {
                    "DEPENDENCIES_UNREADY",
                    "PENDING_CREATION",
                    "RESTARTING",
                }:
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
                    "replica_id": deployment.replica_id,
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
                            f"{deployment.model_key}:{deployment.replica_id}": {
                                "gpus_required": len(deployment.gpus),
                            }
                            for deployment in node.deployments.values()
                        },
                    }
                    for node_id, node in self.cluster.nodes.items()
                }
            },
        }


@ray.remote(num_cpus=1, num_gpus=0, max_restarts=-1, resources={"head": 1})
class ControllerActor(_ControllerActor):
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

def app(**kwargs):
    args = ControllerDeploymentArgs(**kwargs)

    actor = ControllerActor.options(
        name="Controller",
        namespace="NDIF",
        lifetime="detached",
        runtime_env={
            **SioProvider.to_env(),
            **ObjectStoreProvider.to_env(),
            **MailgunProvider.to_env(),
        },
    ).remote(**args.model_dump())
