"""Ray Serve model deployment implementation.

This module provides the ModelDeployment class for serving machine learning models
using Ray Serve, with support for caching, health checks, and actor management.
"""

import os
from typing import Any, Dict

import ray
from ray import serve

from ....logging import set_logger
from ....schema import BackendRequestModel
from .base import BaseModelDeployment, BaseModelDeploymentArgs


@ray.remote(num_cpus=2, num_gpus=0, max_restarts=-1)
class ModelActor(BaseModelDeployment):
    """Ray remote actor for model execution.
    
    This actor handles the actual model inference and is managed by the
    ModelDeployment class. It inherits from BaseModelDeployment to provide
    the core model functionality.
    """
    
    pass
@serve.deployment(
    ray_actor_options={
        "num_cpus": 1,
    },
    max_ongoing_requests=200, max_queued_requests=200,
    health_check_period_s=10000000000000000000000000000000,
    health_check_timeout_s=12000000000000000000000000000000,
)
class ModelDeployment:
    """Ray Serve deployment for machine learning models.
    
    This class manages the deployment of ML models using Ray Serve, including
    actor creation, caching, health checks, and request handling.
    
    Args:
        node_name: Name of the node where the model should be deployed
        cached: Whether to load the model from cache
        model_key: Unique identifier for the model
        **kwargs: Additional arguments passed to the model
    """
    
    def __init__(self, node_name: str, cached: bool, model_key: str, **kwargs: Any) -> None:
        
        super().__init__()

        self.cached = cached
        self.model_key = model_key
        self.node_name = node_name
        self.kwargs = kwargs
        self.logger = set_logger(model_key)
        
        self.cuda_devices = os.environ["CUDA_VISIBLE_DEVICES"]
        
        # Get Ray Serve context
        self.replica_context = serve.get_replica_context()
        self.app = self.replica_context.app_name
        
        # Try to get existing actor or create new one
        try:
            actor = ray.get_actor(f"ModelActor:{self.model_key}")
        except Exception:
            self.logger.info(f"Creating actor {self.model_key}...")
            actor = self.create_model_actor()
        
        # Load from cache if requested
        if self.cached:
            self.logger.info(f"Loading actor {self.model_key} from cache...")
            try:
                ray.get(actor.from_cache.remote(self.cuda_devices, self.app))
            except Exception as e:
                self.logger.error(f"Error getting actor {self.model_key} from cache: {e}")
    
    def create_model_actor(self) -> None:
        """Create a new model actor with specified configuration.
        
        Creates a Ray actor for model execution with resource constraints
        and lifetime management.
        """
        actor = ModelActor.options(
            name=f"ModelActor:{self.model_key}",
            resources={f"node:{self.node_name}": 0.01},
            lifetime="detached"
        ).remote(
            model_key=self.model_key,
            cuda_devices=self.cuda_devices,
            app=self.app,
            **self.kwargs
        )
        ray.get(actor.__ray_ready__.remote())
        
        return actor
    
    async def __call__(self, request: BackendRequestModel) -> Any:
        """Handle incoming requests by delegating to the model actor.
        
        Args:
            request: The incoming request to process
            
        Returns:
            The result from the model actor
        """
        
        actor = ray.get_actor(f"ModelActor:{self.model_key}")
        
        return await actor.__call__.remote(request)
    
    async def cancel(self) -> None:
        """Cancel ongoing operations on the model actor."""
        
        actor = ray.get_actor(f"ModelActor:{self.model_key}")
        
        await actor.cancel.remote()
    
    async def restart(self) -> None:
        """Restart the model actor by killing and recreating it."""
        
        actor = ray.get_actor(f"ModelActor:{self.model_key}")
        
        ray.kill(actor, no_restart=False)

    async def __del__(self) -> None:
        """Cleanup method called when the deployment is destroyed."""
        self.logger.info(f"Deleting model for model key {self.model_key}...")


def app(args: BaseModelDeploymentArgs) -> serve.Application:
    """Create a Ray Serve application from deployment arguments.
    
    Args:
        args: Configuration arguments for the model deployment
        
    Returns:
        A configured Ray Serve application
    """
    return ModelDeployment.bind(**args.model_dump())
