"""Processor module for managing per-model request queues and deployment lifecycle.

This module provides the Processor class which orchestrates the lifecycle of model
deployments, including provisioning, initialization, request queuing, and execution.
Each Processor instance manages requests for a single model and communicates with
the Ray-based backend infrastructure.

Typical usage:
    The Processor is created and managed by the Dispatcher when a new model
    request arrives. The Dispatcher creates a Processor for each unique model_key
    and delegates request handling to it.

Example:
    >>> processor = Processor(
    ...     model_key="meta-llama/Llama-2-7b",
    ...     eviction_queue=eviction_queue,
    ...     error_queue=error_queue
    ... )
    >>> asyncio.create_task(processor.processor_worker())
    >>> processor.enqueue(request)
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Optional

import ray

from ..schema import BackendRequestModel, BackendResponseModel

from .config import QueueConfig
from .util import controller_handle, get_model_actor_handle, submit

logger = logging.getLogger("ndif")


class ProcessorStatus(Enum):
    """Enumeration of possible processor states.

    The processor transitions through these states during its lifecycle:
    UNINITIALIZED -> PROVISIONING -> DEPLOYING -> READY <-> BUSY -> CANCELLED

    Attributes:
        UNINITIALIZED: Initial state before any operations have begun.
        PROVISIONING: Requesting the Controller to create/allocate the model deployment.
        DEPLOYING: Waiting for the model actor to complete initialization.
        READY: Model is loaded and ready to accept requests.
        BUSY: Currently executing a request or waiting for error resolution.
        CANCELLED: Terminal state indicating the processor has been evicted or failed.
    """

    UNINITIALIZED = "uninitialized"
    PROVISIONING = "provisioning"
    DEPLOYING = "deploying"
    READY = "ready"
    BUSY = "busy"
    CANCELLED = "cancelled"


class DeploymentStatus(Enum):
    """Enumeration of deployment states reported by the Controller.

    These states indicate the result of a deployment request to the Controller
    and determine whether the Processor can proceed with initialization.

    Attributes:
        DEPLOYED: Model was successfully deployed and is ready.
        CACHED_AND_FREE: Model is cached in memory and has available capacity.
        FREE: Model slot is available for deployment.
        CACHED_AND_FULL: Model is cached but has no available capacity.
        FULL: No capacity available for this model.
        CANT_ACCOMMODATE: Controller cannot accommodate this model at all
            (e.g., insufficient resources, unsupported model).
    """

    DEPLOYED = "deployed"
    CACHED_AND_FREE = "cached_and_free"
    FREE = "free"
    CACHED_AND_FULL = "cached_and_full"
    FULL = "full"
    CANT_ACCOMMODATE = "cant_accommodate"


class Processor:
    """Orchestrates per-model request queues, deployment lifecycle, and backend communication.

    The Processor is responsible for managing the complete lifecycle of a model deployment,
    from initial provisioning through request execution. Each Processor instance handles
    requests for exactly one model, maintaining its own request queue and coordinating
    with the Ray-based Controller and ModelActor.

    The processor follows this lifecycle:
        1. Created by Dispatcher when first request for a model arrives
        2. Provisions the model deployment via the Controller
        3. Waits for the ModelActor to initialize
        4. Processes requests from its queue sequentially
        5. Reports errors/evictions back to Dispatcher via shared queues

    Attributes:
        model_key: Unique identifier for the model (e.g., "meta-llama/Llama-2-7b").
        queue: Async queue holding pending BackendRequestModel instances.
        eviction_queue: Shared queue for reporting eviction events to Dispatcher.
        error_queue: Shared queue for reporting errors to Dispatcher.
        status: Current ProcessorStatus state.
        status_changed_at: Unix timestamp of the last status transition.
        dedicated: Whether this is a dedicated (scheduled) model deployment.
            None if not yet determined, True/False after check_dedicated().
        current_request_id: ID of the request currently being executed, or None.
        current_request_started_at: Unix timestamp when current request started, or None.

    Example:
        >>> eviction_queue = asyncio.Queue()
        >>> error_queue = asyncio.Queue()
        >>> processor = Processor("gpt2", eviction_queue, error_queue)
        >>> asyncio.create_task(processor.processor_worker())
        >>> processor.enqueue(request)
    """

    def __init__(
        self,
        model_key: str,
        eviction_queue: asyncio.Queue[tuple[str, str, Optional[int]]],
        error_queue: asyncio.Queue[tuple[str, Exception]],
        replica_count: Optional[int] = None,
    ) -> None:
        """Initialize a new Processor for a specific model.

        Args:
            model_key: Unique identifier for the model to manage.
            eviction_queue: Shared async queue for reporting eviction events.
                Tuples of (model_key, reason_message) are placed here.
            error_queue: Shared async queue for reporting errors.
                Tuples of (model_key, exception) are placed here.
        """
        self.model_key = model_key
        self.queue: asyncio.Queue[BackendRequestModel] = asyncio.Queue()
        self.eviction_queue = eviction_queue
        self.error_queue = error_queue
        self._status = ProcessorStatus.UNINITIALIZED
        self.status_changed_at: float = 0

        self.replica_count = (replica_count if replica_count is not None else QueueConfig.processor_replica_count)
        self.replica_ids = list(range(self.replica_count))
        self.in_flight = 0
        self.busy_replicas: set[int] = set()
        self._replica_worker_tasks: dict[int, asyncio.Task] = {}
        self.ready_replicas: set[int] = set()

        self.dedicated: Optional[bool] = None
        self.current_request_ids: dict[int, Optional[str]] = {replica_id: None for replica_id in self.replica_ids}
        self.current_request_started_ats: dict[int, Optional[float]] = {replica_id: None for replica_id in self.replica_ids}


    def remove_replica(self, replica_id: int, message: str) -> None:
        """Remove a replica from the processor. Only metadata on this processor is deleted, the replica deployment is not deleted.

        Args:
            replica_id: The ID of the replica to remove.
            message: The message to send to the user.
        """
        if replica_id not in self.replica_ids:
            return

        self.replica_ids.remove(replica_id)
        self.replica_count = len(self.replica_ids)
        self.current_request_ids.pop(replica_id, None)
        self.current_request_started_ats.pop(replica_id, None)
        self.busy_replicas.discard(replica_id)
        self._replica_worker_tasks.pop(replica_id, None)
        self.ready_replicas.discard(replica_id)
        if self.replica_count == 0:
            self.status = ProcessorStatus.CANCELLED
            self.purge(message)
            return
        # we don't really evict this replica, it's the job of the controller/cluster
        # this is used to delete the metadata for this replica to avoid routing requests to it
        if self.in_flight == 0:
            self.status = ProcessorStatus.READY

    def add_replica(self, replica_id: int) -> None:
        """Add a replica to the processor and start a worker if possible."""
        if replica_id in self.replica_ids:
            return

        self.replica_ids.append(replica_id)
        self.replica_ids.sort()
        self.replica_count = len(self.replica_ids)
        self.current_request_ids[replica_id] = None
        self.current_request_started_ats[replica_id] = None
        if self.status in {ProcessorStatus.READY, ProcessorStatus.BUSY}:
            asyncio.create_task(self.initialize_replica(replica_id))

    async def _decrease_desired_replica(self, replica_id: int) -> None:
        """Permanently decrease desired replicas for a failed replica."""
        try:
            controller = controller_handle()
            await submit(
                controller,
                "evict",
                [self.model_key],
                replica_keys=[(self.model_key, replica_id)],
                cache=True,
            )
        except Exception as e:
            self.error_queue.put_nowait((self.model_key, e))

    @property
    def status(self) -> ProcessorStatus:
        """Get the current processor status.

        Returns:
            The current ProcessorStatus enum value.
        """
        return self._status

    @status.setter
    def status(self, value: ProcessorStatus) -> None:
        """Set the processor status and update the timestamp.

        Automatically records the time of the status change for monitoring
        and debugging purposes.

        Args:
            value: The new ProcessorStatus to set.
        """
        self._status = value
        self.status_changed_at = time.time()

    def get_handle(self, replica_id: int) -> ray.actor.ActorHandle:
        """Get the Ray actor handle for this model's deployment.

        Returns:
            The Ray ActorHandle for the ModelActor serving this model.

        Raises:
            Exception: If the actor cannot be found (e.g., not yet deployed
                or has been evicted).
        """
        return get_model_actor_handle(self.model_key, replica_id)

    def enqueue(self, request: BackendRequestModel) -> None:
        """Add a request to the processing queue.

        Validates that the request can be processed (either the model is dedicated
        or the request has hotswapping enabled), adds it to the queue, and notifies
        the user of their queue position.

        Args:
            request: The inference request to enqueue.

        Note:
            If the model is not dedicated and the request doesn't have hotswapping
            enabled, the request is immediately rejected with an error response
            and not added to the queue.
        """
        if self.dedicated is False and not request.hotswapping:
            request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                "Model is not dedicated and hotswapping is not supported for this API key. See https://nnsight.net/status/ for a list of scheduled models.",
            ).respond()

            return

        self.queue.put_nowait(request)

        request.create_response(
            BackendResponseModel.JobStatus.QUEUED,
            logger,
            f"Moved to position {self.queue.qsize()} in Queue.",
        ).respond()

    async def check_dedicated(self, handle: ray.actor.ActorHandle) -> bool:
        """Check if this model has a dedicated (scheduled) deployment.

        Queries the Controller to determine if the model is scheduled for
        dedicated deployment, which affects whether non-hotswapping requests
        can be processed.

        Args:
            handle: Ray actor handle for the Controller.

        Returns:
            True if the model has a dedicated deployment, False otherwise.
            Returns False if the deployment information cannot be retrieved.
        """
        result = await submit(handle, "get_deployment", self.model_key)

        if result is None:
            return False

        return result.get("dedicated", False)

    async def provision(self) -> None:
        """Provision the model deployment via the Controller.

        This method handles the complete provisioning workflow:
            1. Checks if the model is a dedicated deployment
            2. Filters queue to remove invalid requests (non-hotswap on non-dedicated)
            3. Requests the Controller to deploy the model
            4. Processes deployment results and handles evictions

        The method sets the processor status to CANCELLED if:
            - No valid requests remain after filtering
            - An exception occurs during provisioning
            - The Controller returns an invalid status
            - The Controller cannot accommodate the model

        Raises:
            No exceptions are raised; errors are reported via the error_queue
            and the processor status is set to CANCELLED.

        Note:
            Any models evicted by the Controller to make room for this deployment
            are reported to the eviction_queue for the Dispatcher to handle.
        """
        try:
            controller = controller_handle()

            self.dedicated = await self.check_dedicated(controller)

            if not self.dedicated:
                hotswap = False

                valid_queue = list()

                while not self.queue.empty():
                    request = self.queue.get_nowait()

                    if request.hotswapping:
                        hotswap = True

                        valid_queue.append(request)
                    else:
                        request.create_response(
                            BackendResponseModel.JobStatus.ERROR,
                            logger,
                            "Model is not dedicated and hotswapping is not supported for this API key. See https://nnsight.net/status/ for a list of scheduled models.",
                        ).respond()

                for request in valid_queue:
                    self.queue.put_nowait(request)

                if not hotswap:
                    self.eviction_queue.put_nowait(
                        (
                            self.model_key,
                            "Model is not dedicated and hotswapping is not supported for this API key. See https://nnsight.net/status/ for a list of scheduled models.", None,
                        )
                    )
                    self.status = ProcessorStatus.CANCELLED
                    return

            result = await submit(controller, "deploy", [self.model_key], self.replica_count)

        except Exception as e:
            self.eviction_queue.put_nowait(
                (
                    self.model_key,
                    "Error provisioning model deployment. Please try again later. Sorry for the inconvenience.", None,
                )
            )
            self.status = ProcessorStatus.CANCELLED
            self.error_queue.put_nowait((self.model_key, e))

            return

        deployment_statuses = result["result"]

        evictions = result["evictions"]

        for model_key, replica_id in evictions:
            self.eviction_queue.put_nowait(
                (
                    model_key,
                    "Model deployment evicted. Please try again later. Sorry for the inconvenience.",
                    replica_id,
                )
            )

        for result_key, status in deployment_statuses.items():
            model_key, replica_id = result_key
            status_str = str(status).lower()

            try:
                deployment_status = DeploymentStatus(status_str)

            except ValueError:
                self.eviction_queue.put_nowait(
                    (
                        model_key,
                        f"{status_str}\n\nThere was an error provisioning the model deployment. Please try again later. Sorry for the inconvenience.",
                        replica_id,
                    )
                )
                await self._decrease_desired_replica(replica_id)
                self.remove_replica(
                    replica_id,
                    "Error provisioning model deployment. Please try again later. Sorry for the inconvenience.",
                )
                if self.status == ProcessorStatus.CANCELLED:
                    return
                continue

            if deployment_status == DeploymentStatus.CANT_ACCOMMODATE:
                self.eviction_queue.put_nowait(
                    (
                        model_key,
                        "Model deployment cannot be accomodated at this time. Please try again later. Sorry for the inconvenience.",
                        replica_id,
                    )
                )
                await self._decrease_desired_replica(replica_id)
                self.remove_replica(
                    replica_id,
                    "Model deployment cannot be accomodated at this time. Please try again later. Sorry for the inconvenience.",
                )
                if self.status == ProcessorStatus.CANCELLED:
                    return

    async def initialize_replica(self, replica_id: int) -> None:
        """Wait for the model replica deployment to complete initialization.

        Polls the ModelActor until it reports ready via the __ray_ready__ method.
        This method blocks until the model is fully loaded and ready to accept
        inference requests.

        The method handles two cases:
            - Actor not yet created: Continues polling until it appears
            - Initialization error: Reports error to user and cancels processor

        Raises:
            No exceptions are raised; errors are reported via the error_queue
            and the processor status is set to CANCELLED.
        """
        start = time.monotonic()

        while True:
            try:
                logger.info(f"Checking if replica {replica_id} is ready")
                handle = self.get_handle(replica_id)
                logger.info(f"Handle: {handle}")

                await submit(handle, "__ray_ready__")

                self.ready_replicas.add(replica_id)
                if self.status in {ProcessorStatus.READY, ProcessorStatus.BUSY}:
                    self._start_replica_worker(replica_id)
                return  # Success - model is ready

            except Exception as e:
                error_str = str(e)

                # Actor doesn't exist yet - keep waiting
                if error_str.startswith("Failed to look up actor"):
                    await asyncio.sleep(1)
                    continue

                # Actual initialization error - report to user and stop
                self.eviction_queue.put_nowait(
                    (
                        self.model_key,
                        "Error initializing model deployment. Please try again later. Sorry for the inconvenience.",
                        replica_id,
                    )
                )
                self.error_queue.put_nowait((self.model_key, e))
                await self._decrease_desired_replica(replica_id)
                self.remove_replica(
                    replica_id,
                    f"Error initializing model deployment for replica {replica_id}. Please try again later. Sorry for the inconvenience.",
                )
                return

    async def initialize(self) -> None:
        """Wait for enough model replicas to complete initialization."""
        if not self.replica_ids:
            self.status = ProcessorStatus.CANCELLED
            self.purge("No replicas available to initialize. Please try again later. Sorry for the inconvenience.")
            return

        tasks_by_replica = {
            replica_id: asyncio.create_task(self.initialize_replica(replica_id))
            for replica_id in list(self.replica_ids)
        }

        while True:
            min_ready = min(self.replica_count, QueueConfig.processor_min_ready_replicas)
            if len(self.ready_replicas) >= min_ready:
                return
            if self.status == ProcessorStatus.CANCELLED:
                return

            done, _ = await asyncio.wait(
                tasks_by_replica.values(), return_when=asyncio.FIRST_COMPLETED
            )

            # Clean up completed tasks from the tracking map
            for replica_id, task in list(tasks_by_replica.items()):
                if task in done:
                    tasks_by_replica.pop(replica_id, None)

            if not tasks_by_replica and len(self.ready_replicas) < min_ready:
                self.status = ProcessorStatus.CANCELLED
                self.purge(
                    "Model deployment failed to initialize. Please try again later. Sorry for the inconvenience."
                )
                return

    async def execute(self, request: BackendRequestModel, replica_id: int) -> None:
        """Execute a single inference request on the model deployment.

        Submits the request to the ModelActor, notifies the user of dispatch,
        waits for completion, and handles any errors that occur during execution.

        Args:
            request: The inference request to execute.

        Note:
            This method updates the processor's current_request_id and
            current_request_started_at for monitoring purposes. These are
            cleared in the finally block regardless of success or failure.

            On success, the processor transitions to READY status.
            On actor lookup failure, the processor is cancelled and evicted.
            On other errors, the error is reported but the processor remains
            BUSY until the Dispatcher clears the error.
        """
        if replica_id not in self.replica_ids:
            raise Exception(f"Replica ID {replica_id} not found in processor {self.model_key}")
        self.current_request_ids[replica_id] = request.id
        self.current_request_started_ats[replica_id] = time.time()

        try:
            handle = self.get_handle(replica_id)

            request.create_response(
                BackendResponseModel.JobStatus.DISPATCHED,
                logger,
                "Your job has been sent to the model deployment.",
            ).respond()

            result = submit(handle, "__call__", request)

            result = await result

        except Exception as e:
            request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                "Error submitting request to model deployment. Please try again later. Sorry for the inconvenience.",
            ).respond()

            if str(e).startswith("Failed to look up actor"):
                self.eviction_queue.put_nowait(
                    (
                        self.model_key,
                        "Model deployment evicted. Please try again later. Sorry for the inconvenience.",
                        replica_id,
                    )
                )
                await self._decrease_desired_replica(replica_id)
                self.status = ProcessorStatus.CANCELLED
            else:
                self.error_queue.put_nowait((self.model_key, e))

        finally:
            self.current_request_ids[replica_id] = None
            self.current_request_started_ats[replica_id] = None

    async def processor_worker(self, provision: bool = True) -> None:
        """Main asyncio task managing the processor lifecycle and request loop.

        This is the primary entry point for processor operation. It handles:
            1. Spawning the reply_worker for status updates
            2. Provisioning the model deployment (if requested)
            3. Waiting for model initialization
            4. Processing requests from the queue sequentially

        Args:
            provision: If True, request a new deployment from the Controller.
                If False, assume the model is already deployed (used when
                responding to external deployment events).

        Note:
            This method runs indefinitely until the processor status becomes
            CANCELLED. It should be run as an asyncio task via create_task().

            When in BUSY status after an error, the method waits for the
            Dispatcher to clear the error before processing new requests.
        """
        self.status = ProcessorStatus.PROVISIONING

        asyncio.create_task(self.reply_worker())

        if provision:
            await self.provision()
        else:
            self.status = ProcessorStatus.READY

        if self.status == ProcessorStatus.CANCELLED:
            return

        self.status = ProcessorStatus.DEPLOYING

        await self.initialize()

        if self.status == ProcessorStatus.CANCELLED:
            return

        self.status = ProcessorStatus.READY

        for replica_id in self.ready_replicas:
            self._start_replica_worker(replica_id)
        await asyncio.gather(*self._replica_worker_tasks.values())

    def _start_replica_worker(self, replica_id: int) -> None:
        task = self._replica_worker_tasks.get(replica_id)
        if task is not None and not task.done():
            return
        self._replica_worker_tasks[replica_id] = asyncio.create_task(
            self.replica_worker(replica_id)
        )

    async def replica_worker(self, replica_id: int) -> None:
        """Worker loop for a single replica."""
        while self.status != ProcessorStatus.CANCELLED:
            if replica_id not in self.replica_ids:
                return

            request = await self.queue.get()
            # for now, there's no real multi-threading or multi-process
            # there's no data race here
            # TODO: consider race conditions if further scaling is needed
            self.in_flight += 1
            self.busy_replicas.add(replica_id)
            self.status = ProcessorStatus.BUSY

            self.reply()
            try: 
                await self.execute(request, replica_id)
            finally:
                self.in_flight -= 1
                self.busy_replicas.discard(replica_id)
                if self.in_flight == 0 and self.status != ProcessorStatus.CANCELLED:
                    self.status = ProcessorStatus.READY

    async def reply_worker(self) -> None:
        """Asyncio task that sends periodic status updates to queued users.

        Runs during the PROVISIONING and DEPLOYING phases, sending status
        messages to all users in the queue at a configurable interval.
        Exits once the processor reaches READY or CANCELLED status.

        See Also:
            QueueConfig.processor_reply_freq_s: Configures the update interval.
        """
        reply_freq_s = QueueConfig.processor_reply_freq_s

        while (
            self.status != ProcessorStatus.READY
            and self.status != ProcessorStatus.CANCELLED
        ):
            if self.status == ProcessorStatus.PROVISIONING:
                self.reply("Model Provisioning...")
            elif self.status == ProcessorStatus.DEPLOYING:
                self.reply("Model Deploying...")

            await asyncio.sleep(reply_freq_s)

    def reply(
        self,
        description: Optional[str] = None,
        status: BackendResponseModel.JobStatus = BackendResponseModel.JobStatus.QUEUED,
    ) -> None:
        """Send a status message to all users currently in the queue.

        Iterates through all pending requests and sends a response with either
        the provided description or a default queue position message.

        Args:
            description: Custom message to send to all users. If None, each user
                receives their current queue position (e.g., "Moved to position 2 in Queue.").
            status: The job status to report. Defaults to QUEUED.
        """
        for i, request in enumerate(self.queue._queue):
            request.create_response(
                status,
                logger,
                (
                    description
                    if description is not None
                    else f"Moved to position {i + 1} in Queue."
                ),
            ).respond()

    def purge(self, message: Optional[str] = None) -> None:
        """Send an error message to all queued users and clear the queue.

        Used during processor shutdown or critical errors to notify all
        pending users that their requests cannot be processed.

        Args:
            message: Error message to send to all users. If None, a default
                critical server error message is used.
        """
        if message is None:
            message = "Critical server error occurred. Please try again later. Sorry for the inconvenience."

        self.reply(message, status=BackendResponseModel.JobStatus.ERROR)

    def get_state(self) -> dict[str, object]:
        """Get a snapshot of the current processor state.

        Returns:
            A dictionary containing:
                - model_key: The model identifier string.
                - status: Current ProcessorStatus value as string.
                - status_changed_at: Unix timestamp of last status change.
                - request_ids: List of request IDs currently in the queue.
                - current_request_ids: Dictionary of request IDs currently being executed.
                - current_request_started_ats: Dictionary of Unix timestamps when current
                    requests started, or None.
                - dedicated: Whether this is a dedicated deployment
                    (True/False/None).
        """
        request_ids = [req.id for req in self.queue._queue]

        return {
            "model_key": self.model_key,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at,
            "request_ids": request_ids,
            "current_request_ids": self.current_request_ids,
            "current_request_started_ats": self.current_request_started_ats,
            "in_flight": self.in_flight,
            "replica_count": self.replica_count,
            "replica_ids": self.replica_ids,
            "dedicated": self.dedicated,
        }

    async def kill_request(self, request_id: str) -> dict:
        """Kill a specific request by ID.

        Args:
            request_id: The ID of the request to kill

        Returns:
            dict with status: "not_found", "removed_from_queue", or "cancelled_execution"
        """
        for replica_id, current_request_id in self.current_request_ids.items():
        # Check if it's the currently executing request
            if current_request_id == request_id:
                try:
                    handle = self.get_handle(replica_id)
                    await submit(handle, "cancel")
                    return {
                        "status": "cancelled_execution",
                        "message": f"Cancelled executing request {request_id}",
                    }
                except Exception as e:
                    return {
                        "status": "error",
                        "message": f"Error cancelling request: {str(e)}",
                    }

        # Check if it's in the queue
        found_request = None
        for request in self.queue._queue:
            if request.id == request_id:
                found_request = request
                break

        if found_request:
            # Remove from queue
            self.queue._queue.remove(found_request)

            # Notify the user their request was cancelled
            found_request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                "Request cancelled.",
            ).respond()

            # Update remaining requests in queue about new positions
            self.reply()

            return {
                "status": "removed_from_queue",
                "message": f"Removed request {request_id} from queue",
            }

        # Not found
        return {
            "status": "not_found",
            "message": f"Request {request_id} not found in processor {self.model_key}",
        }
