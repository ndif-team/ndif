import os
import ray
import time
import threading
from collections import defaultdict
from functools import wraps
from ray import serve
from slugify import slugify
from typing import Dict, List
from ..schema import BackendRequestModel
from ..queue_manager import QueueManager
from ..logging import load_logger
from .base import BaseDispatcher

logger = load_logger(service_name="Dispatcher", logger_name="Dispatcher")
poll_interval = float(os.getenv("DISPATCHER_POLL_INTERVAL", "1.0"))

def with_model_key(func):
    """Decorator that converts task_source to model_key and passes it as the second argument."""
    @wraps(func)
    async def wrapper(self, task_source: str, *args, **kwargs):
        model_key = f"Model:{slugify(task_source)}"
        return await func(self, task_source, model_key, *args, **kwargs)
    return wrapper

class RayDispatcher(BaseDispatcher):
    """Dispatches requests to Ray cluster with proper resource management."""
    
    def __init__(self, queue_manager: QueueManager, ray_url: str, poll_interval: float = poll_interval):
        super().__init__(poll_interval)
        self.queue_manager = queue_manager
        self.ray_url = ray_url
        self.ray_watchdog = threading.Thread(target=RayDispatcher.connect_to_ray, args=(self.ray_url,), daemon=True)
        self.ray_watchdog.start()
        self._dispatched_requests: Dict[str, List[ray.ObjectRef]] = defaultdict(list)
    
    @staticmethod
    def connect_to_ray(ray_url: str):
        """Connect to Ray cluster."""
        retry_interval = int(os.environ.get("RAY_RETRY_INTERVAL_S", 5))
        while True:
            try:
                if not ray.is_initialized():
                    ray.shutdown()
                    serve.context._set_global_client(None)
                    ray.init(logging_level="error", address=ray_url)
                    logger.info("Connected to Ray cluster.")
                    return
            except Exception as e:
                logger.error(f"Failed to connect to Ray cluster: {e}")
            time.sleep(retry_interval)
    
    async def get_task_sources(self) -> List[str]:
        """Get all model keys that should be checked for dispatching."""
        return list(self.queue_manager.keys())
    
    @with_model_key
    async def has_pending_tasks(self, task_source: str, model_key: str) -> bool:
        """Check if we can dispatch a new request for the given model key."""
        # Simple check. handle_idle_task_source() is responsible for updating _dispatched_requests.
        if model_key not in self._dispatched_requests:
            logger.debug(f"No active request for {model_key}, can dispatch")
            return True

        return False
    
    async def get_next_batch(self, task_source: str) -> List[BackendRequestModel]:
        """Get the next batch of tasks to dispatch for the given model key."""
        task = self.queue_manager.dequeue(task_source)
        return [task] if task else []
    
    async def dispatch(self, task: BackendRequestModel):
        """Dispatch a single task."""
        model_key = f"Model:{slugify(task.model_key)}"
        deployment = serve.get_app_handle(model_key)
        if not deployment:
            raise ValueError(f"Ray Serve app '{model_key}' not found")
        
        task.graph = ray.put(task.graph)
        handle = deployment.remote(task)
        self._dispatched_requests[model_key].append(await handle._to_object_ref())
        logger.info(f"Dispatcher dispatched request {task.id} to {model_key}")

        self.queue_manager.state[model_key].dispatch_request(task.id)
    
    async def dispatch_batch(self, batch: List[BackendRequestModel]):
        """Dispatch pending tasks for the given model key."""
        if not batch:
            logger.warning(f"Batch is empty, skipping dispatch")
            return
        
        try:
            for task in batch:
                await self.dispatch(task)
        except Exception as e:
            logger.error(f"Error dispatching batch: {e}")
            raise

    @with_model_key
    async def handle_idle_task_source(self, task_source: str, model_key: str):
        """Clean up empty dispatched requests for a model key."""
        handles = self._dispatched_requests.get(model_key, [])
        if not handles:
            # This should never happen, but just in case.
            raise ValueError(f"No handles found for {model_key}")
 
        try:
            ready, not_ready = ray.wait(handles, num_returns=len(handles), timeout=0)
            if len(ready) == len(handles):
                # The model is free!
                del self._dispatched_requests[model_key]
                self.queue_manager.delete_if_empty(model_key)
                self.queue_manager.state[model_key].clear_dispatched()
                
            else:
                logger.debug(f"{len(not_ready)} requests for {model_key} still running")
        except Exception as e:
            logger.warning(f"Error checking Ray ObjectRefs for {model_key}: {e}")
            del self._dispatched_requests[model_key]

        
# For backward compatibility
Dispatcher = RayDispatcher 