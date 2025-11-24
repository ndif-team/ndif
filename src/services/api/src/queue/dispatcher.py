import asyncio
import os
import pickle
import time
import redis

from ..logging import set_logger
from ..providers.ray import RayProvider
from ..providers.objectstore import ObjectStoreProvider
from ..schema import BackendRequestModel
from .processor import Processor, ProcessorStatus
from .util import patch, controller_handle, submit


class Dispatcher:

    def __init__(self):
        self.redis_client = redis.asyncio.Redis.from_url(os.environ.get("BROKER_URL"))
        self.processors: dict[str, Processor] = {}

        self.error_queue = asyncio.Queue()
        self.eviction_queue = asyncio.Queue()
        
        self.cached_status = None
        self.last_status_time = 0
        self.status_cache_freq_s = int(
            os.environ.get("COORDINATOR_STATUS_CACHE_FREQ_S", "120")
        )
        
        self.logger = set_logger("coordinator")
        
        patch()

        self.connect()

        ObjectStoreProvider.connect()

    @classmethod
    def start(cls):
        dispatcher = cls()
        asyncio.run(dispatcher.dispatch_worker())
        
    def connect(self):
        
        
        self.logger.info(f"Connecting to Ray")
        
        while not RayProvider.connected():

            try:

                RayProvider.reset()
                RayProvider.connect()

            except Exception as e:
                self.logger.error(f"Error connecting to Ray: {e}")
                
                time.sleep(1)
                
        self.logger.info(f"Connected to Ray")

    async def get(self):

        result = await self.redis_client.brpop("queue", timeout=1)

        if result is not None:
            return pickle.loads(result[1])

    def dispatch(self, request: BackendRequestModel):
        """Route a request to the per-model processor, creating it if missing."""

        if request.model_key not in self.processors:

            processor = Processor(request.model_key, self.eviction_queue, self.error_queue)

            self.processors[request.model_key] = processor

            asyncio.create_task(processor.processor_worker())

        self.processors[request.model_key].enqueue(request)
        
        
    def remove(self, model_key: str, message: str):
        self.logger.error(f"Removing processor {model_key} with status {self.processors[model_key].status}")
        processor = self.processors.pop(model_key)
        processor.status = ProcessorStatus.CANCELLED
        processor.purge(message)
        
    def purge(self, message: str):
        for model_key in list(self.processors.keys()):
            self.remove(model_key, message)
            
    def handle_evictions(self):
        while not self.eviction_queue.empty():
            
            model_key, reason = self.eviction_queue.get_nowait()
            
            try:
                self.remove(model_key, reason)
            except:
                self.logger.exception(f"Error handling eviction for `{model_key}`")

    def handle_errors(self):
        
        if not self.error_queue.empty():
            
            if not RayProvider.connected():    
                
                self.purge("Critical server error occurred. Please try again later. Sorry for the inconvenience.")            

                self.connect()
                
            while not self.error_queue.empty():
                model_key, error = self.error_queue.get_nowait()
                self.logger.error(f"Error in model {model_key}: {error}")
                
                if model_key in self.processors:
                    processor = self.processors[model_key]
                    processor.status = ProcessorStatus.READY
            
    async def dispatch_worker(self):
        """Main asyncio task for monitoring the dispatch queue and routing requests to the appropriate processors.
        """
        
        asyncio.create_task(self.status_worker())
        
        while True:
            
            # Get the next request from the queue.
            request = await self.get()
            if request is not None:
                
                # Dispatch the request to the appropriate processor.
                self.dispatch(request)

            # Handle any evictions or errors that may have been added by the processors.   
            self.handle_evictions()
            self.handle_errors()
            
    async def status_worker(self) -> None:
        """Asyncio task for responding to requests for cluster status
        """
        while True:
            
            id = (await self.redis_client.brpop("status"))[1]
            
            if time.time() - self.last_status_time > self.status_cache_freq_s:
                
                try:
                    
                    handle = controller_handle()
                    
                    self.cached_status = await submit(handle, "status")
                    
                except Exception as e:
                    
                    self.logger.error(f"Error getting status: {e}")
                    
                    continue
                
                else:
                    
                    self.cached_status = pickle.dumps(self.cached_status)
                    
                    self.last_status_time = time.time()
                
            await self.redis_client.lpush(id, self.cached_status)
                