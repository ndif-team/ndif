import asyncio
import os
import ray
import time
import threading
from ray import serve
from slugify import slugify
from typing import Dict, Union
from .schema import BackendRequestModel
from nnsight.schema.response import ResponseModel
from .queue_manager import QueueManager
import logging
logger = logging.getLogger("ndif")

poll_interval = float(os.getenv("DISPATCHER_POLL_INTERVAL", "1.0"))

def connect_to_ray(ray_url: str = None):
    """Connect to Ray cluster."""
    retry_interval = int(os.environ.get("RAY_RETRY_INTERVAL_S", 5))
    
    while True:
        try:
            if not ray.is_initialized():
                ray.shutdown()
                serve.context._set_global_client(None)
                ray.init(logging_level="error", address=ray_url)
                logger.info("Connected to Ray cluster.")
                return  # Exit after successful connection
        except Exception as e:
            logger.error(f"Failed to connect to Ray cluster: {e}")
            
        time.sleep(retry_interval)
        
class Dispatcher:
    """Dispatches requests to Ray cluster with proper resource management."""
    
    def __init__(self, queue_manager: QueueManager, ray_url: str, poll_interval: float = poll_interval):
        self.queue_manager = queue_manager
        self.ray_url = ray_url
        self.poll_interval = poll_interval
        self.running = False

        # Start the background thread to connect to Ray
        self.ray_watchdog = threading.Thread(target=connect_to_ray, args=(self.ray_url,), daemon=True)
        self.ray_watchdog.start()

        self._task = None
        # Store Ray ObjectRefs or asyncio Tasks for dispatched requests
        self._dispatched_requests: Dict[str, Union[ray.ObjectRef, asyncio.Task]] = {}
        
        self._controller = None
        
    @property
    def controller(self):
        if self._controller is None:
            self._controller = serve.get_app_handle("Controller")
        return self._controller

    async def start(self):
        """Start the dispatcher loop."""
        if self.running:
            return
        
        self.running = True
        self._task = asyncio.create_task(self.dispatch_loop())
        logger.info("Dispatcher started")

    async def stop(self):
        """Stop the dispatcher loop."""
        if not self.running:
            return
            
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Dispatcher stopped")

    async def dispatch_loop(self):
        """Main loop to dispatch requests to Ray."""
        while self.running:
            try:
                await self.check_and_dispatch()
            except Exception as e:
                logger.error(f"Error in dispatch loop: {e}")
            finally:
                # Sleep after checking all models to prevent excessive polling
                await asyncio.sleep(self.poll_interval)

    async def check_and_dispatch(self):
        """Check all model queues and dispatch requests to Ray."""
        try:
            model_keys = self.queue_manager.get_all_model_keys()
            for model_key in model_keys:
                queue_length = self.queue_manager.get_queue_length(model_key)
                if queue_length > 0:
                    logger.debug(f"Current queue size for {model_key}: {queue_length}")
                    
                    # First check if we can dispatch
                    if not await self.can_dispatch_for_model(model_key):
                        logger.debug(f"Cannot dispatch for {model_key} - another task is running")
                        continue
                        
                    # If we can dispatch, get the task
                    task = self.queue_manager.dequeue(model_key)
                    try:
                        await self.dispatch_request(model_key, task)
                    except Exception as e:
                        # TODO: Figure out how to handle this: Should we try dispatching again, requeue the request, or drop it?
                        logger.error(f"Failed to dispatch request {task.id} to {model_key}: {e}")

        except Exception as e:
            logger.error(f"Error in check_and_dispatch: {e}")

    async def can_dispatch_for_model(self, model_key: str) -> bool:
        """Check if we can dispatch a new request for the given model key."""
        ray_model_key = f"Model:{slugify(model_key)}"
        
        if ray_model_key not in self._dispatched_requests:
            logger.debug(f"No active request for {ray_model_key}, can dispatch")
            return True
        
        handle_or_task = self._dispatched_requests[ray_model_key]
        
        try:
            ready, _ = ray.wait([handle_or_task], timeout=0)
            if ready:
                # Task is complete, allow new dispatch
                return True
            else:
                logger.debug(f"Request for {ray_model_key} still running")
                return False
        except Exception as e:
            logger.warning(f"Error checking Ray ObjectRef for {ray_model_key}: {e}")
            del self._dispatched_requests[ray_model_key]
            return True
        
      
        
        logger.warning(f"Unknown handle type for {ray_model_key}: {type(handle_or_task)}")
        del self._dispatched_requests[ray_model_key]
        return True

    async def dispatch_request(self, model_key: str, task: BackendRequestModel):
        """Dispatch a request to Ray."""
        try:
           
            ray_model_key = f"Model:{slugify(task.model_key)}"
            
            # Get the Ray Serve handle for the model
            deployment = serve.get_app_handle(ray_model_key)
            if not deployment:
                raise ValueError(f"Ray Serve app '{ray_model_key}' not found")
            
            # Dispatch the request and store the ObjectRef
            handle = deployment.remote(task)
            self._dispatched_requests[ray_model_key] = await handle._to_object_ref()
            
            logger.info(f"Dispatched request {task.id} to {ray_model_key}")
            
        except Exception as e:
            logger.error(f"Error dispatching request {task.id} to {model_key}: {e}")
            
            #TODO say the model isnt available / tell the controller
            # task.create_response(
            #     status=ResponseModel.JobStatus.ERROR,
            #     description=f"Failed to dispatch request {task.id} to {model_key}: {e}",
            #     logger=logger,
            # )
            
            
            # TODO: This should probably be batched instead of doing it per request
            
            self.controller.deploy.remote([model_key])
            
            self.queue_manager.enqueue(task)