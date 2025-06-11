import os
import ray
import time
import threading
from collections import defaultdict
from ray import serve
from slugify import slugify
from typing import Dict, List
from ..schema import BackendRequestModel
from ..queue_manager import QueueManager
from ..logging import load_logger
from .base import BaseDispatcher

logger = load_logger(service_name="Dispatcher", logger_name="Dispatcher")
poll_interval = float(os.getenv("DISPATCHER_POLL_INTERVAL", "1.0"))

class RayDispatcher(BaseDispatcher):
    def __init__(self, queue_manager: QueueManager, ray_url: str, poll_interval: float = poll_interval):
        super().__init__(poll_interval)
        self.queue_manager = queue_manager
        self.ray_url = ray_url
        self.ray_watchdog = threading.Thread(target=RayDispatcher.connect_to_ray, args=(self.ray_url,), daemon=True)
        self.ray_watchdog.start()
        self._dispatched_requests: Dict[str, List[ray.ObjectRef]] = defaultdict(list)
    @staticmethod
    def connect_to_ray(ray_url: str):
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
        return list(self.queue_manager.keys())
    async def has_pending_tasks(self, task_source: str) -> bool:
        model_key = f"Model:{slugify(task_source)}"
        if model_key not in self._dispatched_requests:
            logger.debug(f"No active request for {model_key}, can dispatch")
            return True
        handles = self._dispatched_requests[model_key]
        if not handles:
            del self._dispatched_requests[model_key]
            return True
        try:
            ready, not_ready = ray.wait(handles, num_returns=len(handles), timeout=0)
            if len(ready) == len(handles):
                del self._dispatched_requests[model_key]
                return True
            else:
                logger.debug(f"{len(not_ready)} requests for {model_key} still running")
                return False
        except Exception as e:
            logger.warning(f"Error checking Ray ObjectRefs for {model_key}: {e}")
            del self._dispatched_requests[model_key]
            return True
    async def get_next_batch(self, task_source: str) -> List[BackendRequestModel]:
        task = self.queue_manager.dequeue(task_source)
        return [task] if task else []
    async def dispatch(self, task: BackendRequestModel):
        model_key = f"Model:{slugify(task.model_key)}"
        deployment = serve.get_app_handle(model_key)
        if not deployment:
            raise ValueError(f"Ray Serve app '{model_key}' not found")
        task.graph = ray.put(task.graph)
        handle = deployment.remote(task)
        self._dispatched_requests[model_key].append(await handle._to_object_ref())
        logger.info(f"Dispatcher dispatched request {task.id} to {model_key}")
    async def dispatch_batch(self, batch: List[BackendRequestModel]):
        if not batch:
            return
        try:
            for task in batch:
                await self.dispatch(task)
        except Exception as e:
            logger.error(f"Error dispatching batch: {e}")
            raise

# For backward compatibility
Dispatcher = RayDispatcher 