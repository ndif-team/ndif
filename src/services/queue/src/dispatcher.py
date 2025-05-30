import asyncio
from .redis_manager import RedisQueueManager
import os
import ray
from ray import serve
from slugify import slugify
from .schema import BackendRequestModel
from .logging import load_logger

logger = load_logger(service_name="Dispatcher", logger_name="Dispatcher")

poll_interval = float(os.getenv("DISPATCHER_POLL_INTERVAL", "1.0"))

class Dispatcher:
    def __init__(self, queue_manager: RedisQueueManager, ray_url: str, poll_interval: float = poll_interval):
        self.queue_manager = queue_manager
        self.ray_url = ray_url
        self.poll_interval = poll_interval
        self.running = False
        self._task = None

    async def start(self):
        """Start the dispatcher loop."""
        if self.running:
            return
        
        self.running = True
        self._task = asyncio.create_task(self.dispatch_loop())

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
                task = self.queue_manager.dequeue(model_key)
                if task:
                    await self.dispatch_request(model_key, task)
        except Exception as e:
            logger.error(f"Error in check_and_dispatch: {e}")

    async def dispatch_request(self, model_key: str, task: BackendRequestModel):
        """Dispatch a request to Ray."""
        try:
            # Upload graph to Ray object store
            if isinstance(task.graph, bytes):
                task.graph = ray.put(task.graph)
                
            model_key = f"Model:{slugify(task.model_key)}"
            actor = serve.get_app_handle(model_key)
            if not actor:
                raise ValueError(f"Actor {model_key} not found")
            
            actor.remote(task)
        except Exception as e:
            logger.error(f"Error in dispatch_request: {e}")
            raise