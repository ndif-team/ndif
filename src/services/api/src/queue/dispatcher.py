"""Dispatcher module for routing requests to per-model Processors.

This module provides the Dispatcher class which serves as the central coordinator
for the NDIF request queue system. The Dispatcher receives incoming inference
requests from Redis, routes them to the appropriate per-model Processor, and
manages the lifecycle of all Processors.

Architecture Overview:
    Redis Queue -> Dispatcher -> Processor(s) -> ModelActor(s)

The Dispatcher also handles:
    - Connection management to Ray and Redis
    - Error handling and recovery
    - Status reporting for cluster monitoring
    - Deployment lifecycle events (deploy/evict)

Typical usage:
    The Dispatcher is started as a standalone process that runs indefinitely,
    processing requests from the Redis queue.

Example:
    >>> Dispatcher.start()  # Blocks indefinitely
"""

import asyncio
import os
import pickle
import time
import traceback
from typing import Optional

import redis

from ..logging import set_logger
from ..providers.ray import RayProvider
from ..providers.objectstore import ObjectStoreProvider
from ..schema import BackendRequestModel
from .config import QueueConfig
from .processor import Processor, ProcessorStatus
from .util import patch, controller_handle, submit


class Dispatcher:
    """Central coordinator for routing requests to per-model Processors.

    The Dispatcher is the main entry point for the NDIF queue system. It manages:
        - Redis connection for receiving incoming requests
        - Per-model Processor instances for request execution
        - Error and eviction handling across all Processors
        - Background workers for status reporting and deployment events

    The Dispatcher runs as a long-lived process with several asyncio tasks:
        - dispatch_worker: Main loop processing incoming requests
        - status_worker: Responds to cluster status queries
        - queue_state_worker: Reports queue state for monitoring
        - deployment_events_worker: Handles external deploy/evict events

    Attributes:
        redis_client: Async Redis client for queue operations.
        processors: Dictionary mapping model_key to Processor instance.
        error_queue: Shared queue where Processors report errors.
        eviction_queue: Shared queue where Processors report evictions.
        status_cache_freq_s: How long to cache cluster status in Redis (seconds).
        logger: Logger instance for this coordinator.

    See Also:
        QueueConfig: Centralized configuration for all queue-related settings.

    Example:
        >>> Dispatcher.start()  # Starts the dispatcher and blocks
    """

    def __init__(self) -> None:
        """Initialize the Dispatcher and establish connections.

        Connects to Redis and Ray, sets up shared queues for inter-Processor
        communication, and applies necessary patches.

        Raises:
            Exception: If Redis connection fails (from redis.asyncio.Redis.from_url).
            Note: Ray connection failures are handled with retry logic in connect().

        See Also:
            QueueConfig: Centralized configuration for all queue settings.
        """
        self.redis_client: redis.asyncio.Redis = redis.asyncio.Redis.from_url(
            QueueConfig.broker_url
        )
        self.processors: dict[str, Processor] = {}

        self.error_queue: asyncio.Queue[tuple[str, Exception]] = asyncio.Queue()
        self.eviction_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        self.status_cache_freq_s = QueueConfig.status_cache_freq_s

        self.logger = set_logger("coordinator")

        patch()

        self.connect()

        ObjectStoreProvider.connect()

    @classmethod
    def start(cls) -> None:
        """Create and start the Dispatcher.

        Factory method that instantiates a Dispatcher and runs its main
        dispatch_worker loop. This method blocks indefinitely.

        Note:
            This is the primary entry point for starting the queue system.
            It should be called from the main process/thread.
        """
        dispatcher = cls()
        dispatcher.logger.info(f"Starting dispatcher with PID {os.getpid()}")
        asyncio.run(dispatcher.dispatch_worker())

    def connect(self) -> None:
        """Establish connection to the Ray cluster.

        Attempts to connect to Ray with retry logic. Blocks until a successful
        connection is established, retrying every second on failure.

        Note:
            This method is also called during error recovery when the Ray
            connection is lost.
        """
        self.logger.info(f"Connecting to Ray")

        while not RayProvider.connected():
            try:
                RayProvider.reset()
                RayProvider.connect()

            except Exception as e:
                self.logger.exception("Error connecting to Ray")

                time.sleep(1)

        self.logger.info(f"Connected to Ray")

    async def get(self) -> Optional[BackendRequestModel]:
        """Fetch the next request from the Redis queue.

        Performs a blocking pop on the Redis "queue" with a 1-second timeout.
        This allows the dispatch loop to periodically check for evictions
        and errors even when no requests are arriving.

        Returns:
            The next BackendRequestModel from the queue, or None if the
            timeout elapsed with no request available.
        """
        result = await self.redis_client.brpop("queue", timeout=1)

        if result is not None:
            return pickle.loads(result[1])

        return None

    def dispatch(self, request: BackendRequestModel) -> None:
        """Route a request to the appropriate per-model Processor.

        If no Processor exists for the request's model_key, creates one and
        starts its worker task. Then enqueues the request for processing.

        Args:
            request: The inference request to route.

        Note:
            The Processor is created lazily on first request for a given model.
            The processor_worker task runs concurrently and handles the full
            lifecycle of provisioning, deployment, and request execution.
        """
        if request.model_key not in self.processors:
            processor = Processor(
                request.model_key, self.eviction_queue, self.error_queue
            )

            self.processors[request.model_key] = processor

            asyncio.create_task(processor.processor_worker())

        self.processors[request.model_key].enqueue(request)

    def remove(self, model_key: str, message: str) -> None:
        """Remove a Processor and notify its queued users.

        Removes the Processor from the processors dict, sets its status to
        CANCELLED, and purges all pending requests with the provided error
        message.

        Args:
            model_key: The model identifier of the Processor to remove.
            message: Error message to send to all queued users.

        Raises:
            KeyError: If no Processor exists for the given model_key.
        """
        self.logger.error(
            f"Removing processor {model_key} with status {self.processors[model_key].status}"
        )
        processor = self.processors.pop(model_key)
        processor.status = ProcessorStatus.CANCELLED
        processor.purge(message)

    def purge(self, message: str) -> None:
        """Remove all Processors and notify all queued users.

        Used during critical failures (e.g., Ray disconnection) to clean up
        all active Processors and notify users of the failure.

        Args:
            message: Error message to send to all queued users across all
                Processors.
        """
        for model_key in list(self.processors.keys()):
            self.remove(model_key, message)

    def handle_evictions(self) -> None:
        """Process all pending eviction events from Processors.

        Drains the eviction_queue and removes each evicted Processor,
        notifying their queued users with the eviction reason.

        Note:
            Errors during individual eviction handling are logged but do
            not prevent processing of remaining evictions.
        """
        while not self.eviction_queue.empty():
            model_key, reason = self.eviction_queue.get_nowait()

            try:
                self.remove(model_key, reason)
            except Exception:
                self.logger.exception(f"Error handling eviction for `{model_key}`")

    def handle_errors(self) -> None:
        """Process all pending error events from Processors.

        Drains the error_queue and handles each error:
            - If Ray is disconnected, purges all Processors and reconnects
            - Logs the full traceback for each error
            - Resets affected Processors to READY status to resume processing

        Note:
            Processors remain in BUSY status after reporting an error until
            this method clears them by setting status to READY.
        """
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

    def get_state(self) -> dict[str, dict[str, object]]:
        """Get a snapshot of the dispatcher and all processor states.

        Collects state information from all active Processors for monitoring
        and debugging purposes.

        Returns:
            A dictionary containing:
                - processors: Dict mapping model_key to processor state dict.
                    Each processor state contains model_key, status,
                    status_changed_at, request_ids, current_request_id,
                    current_request_started_at, and dedicated flag.
        """
        processors_state = {
            model_key: processor.get_state()
            for model_key, processor in self.processors.items()
        }

        return {
            "processors": processors_state,
        }

    async def dispatch_worker(self) -> None:
        """Main asyncio task for the dispatch loop.

        This is the primary worker that:
            1. Spawns background workers (status, queue_state, deployment_events)
            2. Continuously fetches requests from Redis
            3. Routes requests to appropriate Processors
            4. Handles evictions and errors between iterations

        Note:
            This method runs indefinitely. Errors in the main loop are logged
            but do not terminate the dispatcher; it continues processing.
        """
        asyncio.create_task(self.status_worker())
        asyncio.create_task(self.queue_state_worker())
        asyncio.create_task(self.deployment_events_worker())

        while True:

            try:
                request = await self.get()
                if request is not None:
                    self.dispatch(request)

                self.handle_evictions()
                self.handle_errors()
            except Exception as e:
                self.logger.exception(f"Error in dispatch worker: {e}")
                continue

    async def status_worker(self) -> None:
        """Asyncio task for responding to cluster status requests.

        Listens for status trigger events on the Redis "status:trigger" stream.
        When triggered, fetches the current cluster status from the Controller
        and publishes it via Redis pub/sub and caches it for future requests.

        The workflow is:
            1. Wait for a trigger event on "status:trigger" stream
            2. Query the Controller for current status
            3. Publish status to "status:event" channel
            4. Cache status in "status" key with TTL
            5. Clear "status:requested" flag

        Note:
            Uses a 60-second timeout when querying the Controller to prevent
            indefinite hangs. Errors are logged but the worker continues.
        """
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

    async def queue_state_worker(self) -> None:
        """Asyncio task for responding to queue state requests.

        Listens for requests on the Redis "queue_state" list. When a request
        arrives (containing a response key), fetches the current dispatcher
        state and pushes the pickled result to the provided response key.

        This enables external monitoring tools to query the current state of
        all Processors and their queues.

        Note:
            On error, an error state dict is returned instead of the normal
            state to inform the requester of the failure.
        """
        while True:
            id = (await self.redis_client.brpop("queue_state"))[1]

            try:
                queue_state = self.get_state()
                queue_state_bytes = pickle.dumps(queue_state)

                await self.redis_client.lpush(id, queue_state_bytes)

            except Exception as e:
                self.logger.error(f"Error getting queue state: {e}")

                error_state = {"error": str(e)}
                await self.redis_client.lpush(id, pickle.dumps(error_state))

    async def deployment_events_worker(self) -> None:
        """Asyncio task for handling external deployment lifecycle events.

        Listens for events on the Redis "deployment_events" list. These events
        are typically sent by the Controller or admin tools to trigger
        deployment actions without a user request.

        Supported event types:
            - "evict": Removes the Processor for the specified model_key,
                notifying queued users that the model was evicted.
            - "deploy": Creates a Processor for a model that was deployed
                externally (skips provisioning since model is already up).

        Event format:
            {
                "type": "evict" | "deploy",
                "model_key": "<model identifier>"
            }

        Note:
            Uses a 1-second timeout on brpop to allow the loop to remain
            responsive. Errors are logged but do not terminate the worker.
        """
        while True:
            try:
                event = await self.redis_client.brpop("deployment_events", timeout=1)

                if event is None:
                    continue

                event_data = pickle.loads(event[1])
                event_type = event_data.get("type")
                model_key = event_data.get("model_key")

                self.logger.info(
                    f"Received deployment event: {event_type} for {model_key}"
                )

                if event_type == "evict":
                    if model_key in self.processors:
                        self.remove(model_key, "Model evicted by external command")
                        self.logger.info(
                            f"Removed processor for {model_key} due to eviction event"
                        )

                elif event_type == "deploy":
                    if model_key not in self.processors:
                        processor = Processor(
                            model_key, self.eviction_queue, self.error_queue
                        )
                        self.processors[model_key] = processor
                        asyncio.create_task(processor.processor_worker(provision=False))
                        self.logger.info(
                            f"Created processor for {model_key} due to deployment event"
                        )

            except Exception as e:
                self.logger.exception(f"Error handling deployment event: {e}")
