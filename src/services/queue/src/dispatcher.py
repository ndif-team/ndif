import asyncio
import os
import ray
import time
import threading
from collections import defaultdict
from abc import ABC, abstractmethod
from ray import serve
from slugify import slugify
from typing import Dict, List, Any
from .schema import BackendRequestModel
from .queue_manager import QueueManager
from .logging import load_logger

logger = load_logger(service_name="Dispatcher", logger_name="Dispatcher")

poll_interval = float(os.getenv("DISPATCHER_POLL_INTERVAL", "1.0"))

class BaseDispatcher(ABC):
    """Abstract base class for dispatchers."""
    
    def __init__(self, poll_interval: float = poll_interval):
        self.poll_interval = poll_interval
        self.running = False
        self._task = None

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
        """Main loop to dispatch requests."""
        while self.running:
            try:
                await self.check_and_dispatch()
            except Exception as e:
                logger.error(f"Error in dispatch loop: {e}")
            finally:
                await asyncio.sleep(self.poll_interval)

    async def check_and_dispatch(self):
        """Check all task sources and dispatch available tasks."""
        try:
            task_sources = await self.get_task_sources()
            for task_source in task_sources:
                # Check if there are pending tasks for this source
                if await self.has_pending_tasks(task_source):
                    try:
                        batch = await self.get_next_batch(task_source)
                        # Skip empty batches to avoid unnecessary dispatch calls
                        if not batch:
                            continue
                    except Exception as e:
                        logger.error(f"Failed to get next batch for {task_source}: {e}")
                        continue

                    await self.dispatch_batch(batch)
                    
                    
        except Exception as e:
            logger.error(f"Error in check_and_dispatch: {e}")

    @abstractmethod
    async def get_task_sources(self) -> List[str]:
        """Get all task sources that should be checked for dispatching."""
        pass

    @abstractmethod
    async def has_pending_tasks(self, task_source: str) -> bool:
        """Check if there are pending tasks for the given task source."""
        pass

    @abstractmethod
    async def get_next_batch(self, task_source: str) -> List[Any]:
        """Get the next batch of tasks to dispatch for the given task source."""
        pass

    @abstractmethod
    async def dispatch_batch(self, batch: List[Any]):
        """Dispatch a batch of tasks."""
        pass
    

class RayDispatcher(BaseDispatcher):
    """Dispatches requests to Ray cluster with proper resource management."""
    
    def __init__(self, queue_manager: QueueManager, ray_url: str, poll_interval: float = poll_interval):
        super().__init__(poll_interval)
        self.queue_manager = queue_manager
        self.ray_url = ray_url

        # Start the background thread to connect to Ray
        self.ray_watchdog = threading.Thread(target=RayDispatcher.connect_to_ray, args=(self.ray_url,), daemon=True)
        self.ray_watchdog.start()

        # Store Ray ObjectRefs for dispatched requests
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
                    return  # Exit after successful connection
            except Exception as e:
                logger.error(f"Failed to connect to Ray cluster: {e}")
                
            time.sleep(retry_interval)


    async def get_task_sources(self) -> List[str]:
        """Get all model keys that should be checked for dispatching."""
        return self.queue_manager.get_all_model_keys()

    async def has_pending_tasks(self, task_source: str) -> bool:
        """Check if we can dispatch a new request for the given model key."""
        model_key = f"Model:{slugify(task_source)}"
        
        # TODO: First check if there are actually tasks in the queue for this source
        #if not self.queue_manager.has_pending_tasks(task_source):
        #    return False
        
        if model_key not in self._dispatched_requests:
            logger.debug(f"No active request for {model_key}, can dispatch")
            return True
        
        handles = self._dispatched_requests[model_key]
        
        if not handles:  # Empty list
            del self._dispatched_requests[model_key]
            return True
        
        try:
            # Check if ALL object refs are completed
            ready, not_ready = ray.wait(handles, num_returns=len(handles), timeout=0)
            if len(ready) == len(handles):
                # All tasks are complete, allow new dispatch
                # TODO: Notification regarding each request moving up the queue
                del self._dispatched_requests[model_key]
                return True
            else:
                logger.debug(f"{len(not_ready)} requests for {model_key} still running")
                return False
        except Exception as e:
            logger.warning(f"Error checking Ray ObjectRefs for {model_key}: {e}")
            del self._dispatched_requests[model_key]
            return True
        logger.warning(f"Unknown handle type for {model_key}: {type(handle)}")
        del self._dispatched_requests[model_key]
        return True

    async def get_next_batch(self, task_source: str) -> List[BackendRequestModel]:
        """Get the next batch of tasks to dispatch for the given model key."""
        # TODO: Have queue manager return a batch instead of single item
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
            

    async def dispatch_batch(self, batch: List[BackendRequestModel]):
        """Dispatch pending tasks for the given model key."""
        if not batch:
            return

        # TODO: If ever we need to dispatch a batch, this logic would ideally be made more efficient
        try:
            for task in batch:
                await self.dispatch(task)
        except Exception as e:
            logger.error(f"Error dispatching batch: {e}")
            raise

# For backward compatibility, keep the original class name as an alias
Dispatcher = RayDispatcher