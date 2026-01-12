import asyncio
import os
import pickle
import time
import redis
import traceback
from enum import Enum

from ..logging import set_logger
from ..providers.ray import RayProvider
from ..providers.objectstore import ObjectStoreProvider
from ..schema import BackendRequestModel
from .processor import Processor, ProcessorStatus
from .util import patch, controller_handle, submit


class DispatcherEvent(str, Enum):
    """Event types for the unified dispatcher events stream"""
    QUEUE_STATE_REQUEST = "queue_state_request"
    DEPLOY = "deploy"
    EVICT = "evict"
    KILL_REQUEST = "kill_request"


class Dispatcher:
    def __init__(self):
        self.redis_client: redis.asyncio.Redis = redis.asyncio.Redis.from_url(
            os.environ.get("BROKER_URL")
        )
        self.processors: dict[str, Processor] = {}

        self.error_queue = asyncio.Queue()
        self.eviction_queue = asyncio.Queue()

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
        dispatcher.logger.info(f"Starting dispatcher with PID {os.getpid()}")
        asyncio.run(dispatcher.dispatch_worker())

    def connect(self):
        self.logger.info(f"Connecting to Ray")

        while not RayProvider.connected():
            try:
                RayProvider.reset()
                RayProvider.connect()

            except Exception as e:
                self.logger.exception("Error connecting to Ray")

                time.sleep(1)

        self.logger.info(f"Connected to Ray")

    async def get(self):
        result = await self.redis_client.brpop("queue", timeout=1)

        if result is not None:
            return pickle.loads(result[1])

    def dispatch(self, request: BackendRequestModel):
        """Route a request to the per-model processor, creating it if missing."""

        if request.model_key not in self.processors:
            processor = Processor(
                request.model_key, self.eviction_queue, self.error_queue
            )

            self.processors[request.model_key] = processor

            asyncio.create_task(processor.processor_worker())

        self.processors[request.model_key].enqueue(request)

    def remove(self, model_key: str, message: str):
        self.logger.error(
            f"Removing processor {model_key} with status {self.processors[model_key].status}"
        )
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
                self.purge(
                    "Critical server error occurred. Please try again later. Sorry for the inconvenience."
                )

                self.connect()

            while not self.error_queue.empty():
                model_key, error = self.error_queue.get_nowait()
                tb_str = "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                )
                self.logger.error(f"Error in model {model_key}: {error}\n{tb_str}")

                if model_key in self.processors:
                    processor = self.processors[model_key]
                    processor.status = ProcessorStatus.READY

    def get_state(self) -> dict:
        """Get the current state of the dispatcher and all processors.
        """
        processors_state = {
            model_key: processor.get_state()
            for model_key, processor in self.processors.items()
        }

        return {
            "processors": processors_state,
        }

    async def dispatch_worker(self):
        """Main asyncio task for monitoring the dispatch queue and routing requests to the appropriate processors."""

        asyncio.create_task(self.status_worker())
        asyncio.create_task(self.events_worker())

        while True:

            try:
                # Get the next request from the queue.
                request = await self.get()
                if request is not None:
                    # Dispatch the request to the appropriate processor.
                    self.dispatch(request)

                # Handle any evictions or errors that may have been added by the processors.
                self.handle_evictions()
                self.handle_errors()
            except Exception as e:
                self.logger.exception(f"Error in dispatch worker: {e}")
                continue

    async def status_worker(self) -> None:
        """Asyncio task for responding to requests for cluster status"""

        last_id = "$"

        got_status = True

        while True:

            try:
                if got_status:
                    message = await self.redis_client.xread(
                        {"status:trigger": last_id}, count=1, block=0
                    )

                    self.logger.info(f"Status trigger received")

                    _, entries = message[0]
                    entry_id, _ = entries[0]

                    got_status = False

                    last_id = entry_id

                handle = controller_handle()

                status = await asyncio.wait_for(submit(handle, "status"), timeout=60)
                status = pickle.dumps(status)

                await self.redis_client.publish("status:event", status)

                await self.redis_client.set(
                    "status", status, ex=self.status_cache_freq_s
                )

                await self.redis_client.delete("status:requested")

                got_status = True

            except Exception as e:
                self.logger.exception(f"Error getting status: {e}")

    async def events_worker(self) -> None:
        """Unified asyncio task for handling all dispatcher events via Redis streams.

        Handles:
        - QUEUE_STATE_REQUEST: Respond with current queue state
        - DEPLOY: Create processor for newly deployed model
        - EVICT: Remove processor for evicted model
        - KILL_REQUEST: Cancel a specific request (future)
        """
        last_id = "$"  # Start from new messages

        while True:
            try:
                # Read from the dispatcher events stream
                messages = await self.redis_client.xread(
                    {"dispatcher:events": last_id},
                    count=1,
                    block=1000  # Block for 1 second
                )

                if not messages:
                    continue

                # Extract stream data
                _, entries = messages[0]

                for entry_id, event_data in entries:
                    last_id = entry_id

                    # Get event type
                    event_type = event_data.get(b"event_type", b"").decode("utf-8")

                    self.logger.info(f"Received event: {event_type}")

                    # Handle different event types
                    if event_type == DispatcherEvent.QUEUE_STATE_REQUEST:
                        await self._handle_queue_state_request(event_data)

                    elif event_type == DispatcherEvent.DEPLOY:
                        await self._handle_deploy_event(event_data)

                    elif event_type == DispatcherEvent.EVICT:
                        await self._handle_evict_event(event_data)

                    elif event_type == DispatcherEvent.KILL_REQUEST:
                        await self._handle_kill_request(event_data)

                    else:
                        self.logger.warning(f"Unknown event type: {event_type}")

            except Exception as e:
                self.logger.exception(f"Error in events worker: {e}")

    async def _handle_queue_state_request(self, event_data: dict) -> None:
        """Handle QUEUE_STATE_REQUEST event"""
        response_key = event_data.get(b"response_key", b"").decode("utf-8")

        try:
            queue_state = self.get_state()
            queue_state_bytes = pickle.dumps(queue_state)
            await self.redis_client.lpush(response_key, queue_state_bytes)

        except Exception as e:
            self.logger.error(f"Error getting queue state: {e}")
            error_state = {"error": str(e)}
            await self.redis_client.lpush(response_key, pickle.dumps(error_state))

    async def _handle_deploy_event(self, event_data: dict) -> None:
        """Handle DEPLOY event"""
        model_key = event_data.get(b"model_key", b"").decode("utf-8")

        if model_key not in self.processors:
            processor = Processor(
                model_key, self.eviction_queue, self.error_queue
            )
            self.processors[model_key] = processor
            asyncio.create_task(processor.processor_worker(provision=False))
            self.logger.info(f"Created processor for {model_key} due to deployment event")

    async def _handle_evict_event(self, event_data: dict) -> None:
        """Handle EVICT event"""
        model_key = event_data.get(b"model_key", b"").decode("utf-8")

        if model_key in self.processors:
            self.remove(model_key, "Model evicted by external command")
            self.logger.info(f"Removed processor for {model_key} due to eviction event")

    async def _handle_kill_request(self, event_data: dict) -> None:
        """Handle KILL_REQUEST event (future implementation)"""
        request_id = event_data.get(b"request_id", b"").decode("utf-8")
        self.logger.info(f"Kill request received for request_id: {request_id}")
        # TODO: Implement request killing logic
        pass
