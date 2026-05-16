"""Processor module for managing per-model request queues and replica pools.

This module provides the Processor class which orchestrates the lifecycle of
a model's deployment, including provisioning, request queuing, and routing
across HOT replicas. Each Processor instance manages requests for a single
``model_key`` and holds a pool of :class:`Replica` instances — the Replica
owns the actor handle, worker task, and per-request dispatch logic; the
Processor handles provisioning, the shared queue, and tear-down when the
pool empties.

Discovery is **lazy**: replicas are learned at provision time via
``Controller.get_deployment`` (and from the response of the
``Controller.deploy`` call when we deploy ourselves). The dispatcher does
not receive explicit replica events from the controller — instead, the
system is robust to staleness: if a Replica dispatches to an actor that has
since been evicted, the resulting "Failed to look up actor" error trips
drift detection inside the Replica, which signals its exit, and the
Processor drops it from its pool. When the last Replica exits the Processor
asks the dispatcher to remove it.

Typical usage:
    The Processor is created and managed by the Dispatcher when a new model
    request arrives.

Example:
    >>> processor = Processor(
    ...     model_key="meta-llama/Llama-2-7b",
    ...     eviction_queue=eviction_queue,
    ...     error_queue=error_queue,
    ... )
    >>> asyncio.create_task(processor.processor_worker())
    >>> processor.enqueue(request)
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Dict, Optional

from opentelemetry import trace

from ....common.providers.ray import controller_handle
from ....common.schema.controller import ReplicaStates
from ....common.schema.request import BackendRequestModel
from ....common.schema.response import BackendResponseModel
from ....common.tracing import trace_span
from ....common.types import MODEL_KEY, REPLICA_ID
from .config import QueueConfig
from .replica import Replica

logger = logging.getLogger("ndif")


class ProcessorStatus(Enum):
    """Enumeration of possible processor states.

    The processor transitions through these states during its lifecycle:
        UNINITIALIZED -> PROVISIONING -> DEPLOYING -> READY -> CANCELLED

    Replica-level busy state is tracked on each :class:`Replica`, so there
    is no global ``BUSY`` here — the Processor is ``READY`` whenever at
    least one replica is serving.

    Attributes:
        UNINITIALIZED: Initial state before any operations have begun.
        PROVISIONING: Looking up existing replicas and/or asking the
            Controller to deploy.
        DEPLOYING: Waiting for at least one replica to mark itself ready.
        READY: At least one replica is up and serving requests.
        CANCELLED: Terminal state. The processor will be removed by the
            dispatcher.
    """

    UNINITIALIZED = "uninitialized"
    PROVISIONING = "provisioning"
    DEPLOYING = "deploying"
    READY = "ready"
    CANCELLED = "cancelled"


class Processor:
    """Orchestrates per-model request queues, replicas, and dispatch.

    Each Processor instance handles requests for exactly one ``model_key``,
    maintaining its own request queue and a pool of :class:`Replica` workers.

    Lifecycle:
        1. Created by Dispatcher when the first request for a model arrives.
        2. ``provision`` discovers existing HOT replicas (via
           ``Controller.get_deployment``) or deploys one if none exist.
        3. ``processor_worker`` waits for the first replica to mark itself
           ready, then transitions to READY.
        4. Replicas pull from the shared queue and dispatch to their actor;
           drift inside a Replica sheds it from the pool.
        5. When the last Replica exits the Processor signals the dispatcher
           via the eviction queue and transitions to CANCELLED.

    Attributes:
        model_key: Unique identifier for the model.
        queue: Async queue holding pending requests; all Replicas pull from
            this queue.
        eviction_queue: Shared queue for reporting eviction events to the
            dispatcher. Tuples of ``(model_key, reason)``.
        error_queue: Shared queue for reporting errors to the dispatcher.
            Tuples of ``(model_key, exception)``.
        pinned: Whether the model is pinned (uniform across replicas).
            ``None`` until ``provision`` resolves it.
        replicas: ``replica_id -> Replica`` for every replica spawned by
            this Processor that hasn't yet exited.
    """

    def __init__(
        self,
        model_key: MODEL_KEY,
        eviction_queue: asyncio.Queue,
        error_queue: asyncio.Queue,
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

        self.pinned: Optional[bool] = None

        self.replicas: Dict[REPLICA_ID, Replica] = {}

        # Set by the first Replica that finishes setup (and also from its
        # ``finally`` on exit, so a fully-failed pool doesn't deadlock the
        # processor_worker).
        self.ready_event: asyncio.Event = asyncio.Event()

    @property
    def status(self) -> ProcessorStatus:
        return self._status

    @status.setter
    def status(self, value: ProcessorStatus) -> None:
        self._status = value
        self.status_changed_at = time.time()

    @property
    def busy(self) -> bool:
        """Whether any replica is currently executing a request."""
        return any(replica.busy for replica in self.replicas.values())

    def abort(self, reason: str) -> None:
        """Mark this processor as cancelled and tell the dispatcher to remove it.

        Idempotent — repeated calls are no-ops once status is CANCELLED.
        Used from every "give up" path: failed provision, no replicas
        placed, exception during setup, last-replica-exit tear-down.
        """
        if self.status == ProcessorStatus.CANCELLED:
            return
        self.eviction_queue.put_nowait((self.model_key, reason))
        self.status = ProcessorStatus.CANCELLED

    async def enqueue(self, request: BackendRequestModel) -> None:
        """Add a request to the processing queue.

        If the model is not pinned and the request doesn't have hotswapping
        enabled, the request is immediately rejected — pinned-vs-hotswap is
        the only access control at this layer.
        """
        if self.pinned is False and not request.hotswapping:
            await request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                "Model is not pinned and hotswapping is not supported for this API key. "
                "See https://nnsight.net/status/ for a list of scheduled models.",
            ).arespond()
            return

        # Stamp the enqueue time so the autoscaling loop can spot the
        # oldest queued request and decide whether to scale up.
        request.enqueued_at = time.time()
        self.queue.put_nowait(request)

        await self.reply(
            request=request,
            description=(
                f"Added to Queue at position {self.queue.qsize()}."
                if self.status
                not in (ProcessorStatus.PROVISIONING, ProcessorStatus.DEPLOYING)
                else None
            ),
        )

    async def provision(self) -> None:
        """Discover (or create) the replicas this processor will serve.

        Always queries the controller for current state first:
            - If replicas exist, populate the pool with them. Pinned-ness is
              taken from the first replica (uniform across siblings).
            - If no replicas exist and any queued requests are hotswap-
              eligible, ask the controller to deploy one replica and spawn
              a Replica for the returned replica_id.
            - If not pinned and no hotswap-eligible requests, the processor
              is rejected via the eviction queue.
        """
        with trace_span(
            "processor.provision", attributes={"ndif.model.key": self.model_key}
        ) as span:
            try:
                controller = controller_handle()

                response: ReplicaStates = await controller.get_deployment.remote(
                    self.model_key
                )
                self.pinned = (
                    response.replicas[0].pinned if response.replicas else False
                )

                span.set_attribute(
                    "ndif.deploy.existing_replicas", len(response.replicas)
                )
                span.set_attribute("ndif.deploy.pinned", self.pinned)

                # If not pinned and the queue already has work, drop any
                # non-hotswap requests up front (and bail entirely if none
                # are hotswap-eligible).
                if not self.pinned and not self.queue.empty():
                    if not await self.filter_hotswap_queue():
                        self.abort(
                            "Model is not pinned and hotswapping is not supported for this API key. "
                            "See https://nnsight.net/status/ for a list of scheduled models."
                        )
                        return
                # Already deployed replicas
                if response.replicas:
                    for state in response.replicas:
                        self.spawn_replica(state.replica_id)
                    return

                # No replicas — deploy one and add it to the pool.
                result = await Replica.deploy(self.model_key, replicas=1)

                if result.error:
                    self.abort(f"Failed to provision model: {result.error}")
                    return

                for replica_id in result.replicas:
                    self.spawn_replica(replica_id)

            except Exception as e:
                span.set_status(trace.StatusCode.ERROR, str(e))
                span.record_exception(e)
                self.abort(
                    "Error provisioning model deployment. "
                    "Please try again later. Sorry for the inconvenience."
                )
                self.error_queue.put_nowait((self.model_key, e))

    async def filter_hotswap_queue(self) -> bool:
        """Drop non-hotswap requests from the queue.

        Returns True if at least one hotswap-eligible request survived
        (or the queue was empty), False if every queued request was rejected.
        """
        hotswap_present = False
        valid = []
        while not self.queue.empty():
            request = self.queue.get_nowait()
            if request.hotswapping:
                hotswap_present = True
                valid.append(request)
            else:
                await request.create_response(
                    BackendResponseModel.JobStatus.ERROR,
                    logger,
                    "Model is not pinned and hotswapping is not supported for this API key. "
                    "See https://nnsight.net/status/ for a list of scheduled models.",
                ).arespond()
        for request in valid:
            self.queue.put_nowait(request)
        return hotswap_present

    def spawn_replica(self, replica_id: REPLICA_ID) -> None:
        replica = Replica(self.model_key, replica_id)
        self.replicas[replica_id] = replica
        replica.start(
            queue=self.queue,
            error_queue=self.error_queue,
            ready_event=self.ready_event,
            on_exit=self.on_replica_exit,
        )

    async def reconcile(self) -> None:
        """Re-sync the replica pool against the controller's current truth.

        Called by the dispatcher when a CLI / dashboard mutation publishes
        a ``RECONCILE_MODEL`` event for our model_key. Diffs
        ``Controller.get_deployment(model_key)`` against the live pool:
            - replica_ids on the controller but not in our pool → spawn
            - replica_ids in our pool but not on the controller → cancel
              (the Replica's on_exit hook handles removal + last-replica
              tear-down)
        Also refreshes ``self.pinned`` since pinned-ness can flip if a
        schedule entry was added or removed.
        """
        with trace_span(
            "processor.reconcile",
            attributes={"ndif.model.key": self.model_key},
        ) as span:
            try:
                response: ReplicaStates = await controller_handle().get_deployment.remote(
                    self.model_key
                )
            except Exception as e:
                span.set_status(trace.StatusCode.ERROR, str(e))
                span.record_exception(e)
                self.error_queue.put_nowait((self.model_key, e))
                return

            desired = {r.replica_id for r in response.replicas}
            have = set(self.replicas.keys())

            for rid in desired - have:
                logger.info(
                    f"Reconcile {self.model_key}: spawning new replica {rid}"
                )
                self.spawn_replica(rid)

            for rid in have - desired:
                logger.info(
                    f"Reconcile {self.model_key}: cancelling stale replica {rid}"
                )
                self.replicas[rid].cancel()

            # pinned-ness can change (e.g. schedule entry added or removed)
            if response.replicas:
                self.pinned = response.replicas[0].pinned

            span.set_attribute("ndif.reconcile.added", len(desired - have))
            span.set_attribute("ndif.reconcile.removed", len(have - desired))

    def on_replica_exit(self, replica: Replica) -> None:
        """Called from each Replica's ``finally`` block as it tears down.

        Removes the Replica from the pool, and — if it was the last live
        replica — signals the dispatcher to tear the Processor down.
        """
        self.replicas.pop(replica.replica_id, None)
        if not self.replicas:
            self.abort(
                "Model deployment evicted. "
                "Please try again later. Sorry for the inconvenience."
            )

    async def processor_worker(self) -> None:
        """Walk the processor through setup, then run the autoscaling loop.

        Replicas do the request-serving work; this coroutine transitions
        the processor from PROVISIONING → DEPLOYING → READY (or short-
        circuits to CANCELLED on a failed provision), then drops into
        ``autoscaling_loop`` which scales the pool up under sustained
        queue pressure. The Processor exits the loop (and the task ends)
        when ``status`` becomes CANCELLED — either because the dispatcher
        purged us or because the last Replica exited.
        """
        self.status = ProcessorStatus.PROVISIONING
        await self.reply()

        await self.provision()

        if self.status == ProcessorStatus.CANCELLED:
            return

        self.status = ProcessorStatus.DEPLOYING
        await self.reply()

        await self.ready_event.wait()

        if self.status == ProcessorStatus.CANCELLED:
            return

        self.status = ProcessorStatus.READY
        await self.reply()

        await self.autoscaling_loop()

    async def autoscaling_loop(self) -> None:
        """Scale the replica pool up under sustained queue pressure.

        Every ``NDIF_AUTOSCALING_INTERVAL_S`` seconds, peek at the head of
        the queue. If its ``enqueued_at`` shows it's been waiting longer
        than ``NDIF_AUTOSCALING_WAIT_THRESHOLD_S``, ask the controller to
        add one more replica and sleep ``NDIF_AUTOSCALING_BACKOFF_S``
        before re-checking — the backoff gives the new replica time to
        come up and drain queue depth before we'd fire another scale-up.
        """
        while self.status != ProcessorStatus.CANCELLED:
            head = self.queue._queue[0] if self.queue._queue else None
            if head is not None and head.enqueued_at is not None:
                wait = time.time() - head.enqueued_at
                if wait > QueueConfig.autoscaling_wait_threshold_s:
                    await self.scale_up(wait)
                    await asyncio.sleep(QueueConfig.autoscaling_backoff_s)
                    continue

            await asyncio.sleep(QueueConfig.autoscaling_interval_s)

    async def scale_up(self, wait: float) -> None:
        """Ask the controller for one more replica and spawn its worker."""
        with trace_span(
            "processor.scale_up",
            attributes={
                "ndif.model.key": self.model_key,
                "ndif.autoscale.queue_head_wait_s": wait,
                "ndif.autoscale.current_replicas": len(self.replicas),
            },
        ) as span:
            logger.info(
                f"Autoscaling {self.model_key}: queue head has waited "
                f"{wait:.1f}s (>{QueueConfig.autoscaling_wait_threshold_s}s); "
                f"adding a replica."
            )
            result = await Replica.deploy(self.model_key, replicas=1)
            if result.error:
                span.set_attribute("ndif.autoscale.error", result.error)
                logger.warning(
                    f"Autoscaling {self.model_key} failed to add replica: "
                    f"{result.error}"
                )
                return
            for rid in result.replicas:
                self.spawn_replica(rid)
            span.set_attribute("ndif.autoscale.new_replicas", len(result.replicas))

    async def reply(
        self,
        request: Optional[BackendRequestModel] = None,
        description: Optional[str] = None,
        status: BackendResponseModel.JobStatus = BackendResponseModel.JobStatus.QUEUED,
    ) -> None:
        """Send a status message to queued users.

        ``request=None`` broadcasts to every request currently in the queue,
        annotating each with its position. Otherwise the message is sent to
        the single given request.
        """
        if description is None:
            if self.status == ProcessorStatus.PROVISIONING:
                description = "Model Provisioning..."
            elif self.status == ProcessorStatus.DEPLOYING:
                description = "Model Deploying..."

        if request is None:
            for i, queued in enumerate(list(self.queue._queue)):
                await queued.create_response(
                    status,
                    logger,
                    (
                        description
                        if description is not None
                        else f"Moved to position {i + 1} in Queue."
                    ),
                ).arespond()
        else:
            await request.create_response(
                status,
                logger,
                description,
            ).arespond()

    async def purge(self, message: Optional[str] = None) -> None:
        """Error every queued request and cancel all replica workers.

        Called by the dispatcher when this processor is being removed
        (either because the last replica exited or because of a critical
        failure).
        """
        if message is None:
            message = (
                "Critical server error occurred. "
                "Please try again later. Sorry for the inconvenience."
            )

        await self.reply(
            description=message, status=BackendResponseModel.JobStatus.ERROR
        )

        for replica in list(self.replicas.values()):
            replica.cancel()

    def get_state(self) -> dict:
        """Snapshot of processor + per-replica state.

        Returns:
            Dict with model_key, status, status_changed_at, request_ids,
            pinned, busy (any replica running), num_replicas, and a
            ``replicas`` list of per-replica state dicts.
        """
        request_ids = [req.id for req in self.queue._queue]

        return {
            "model_key": self.model_key,
            "status": self.status.value,
            "status_changed_at": self.status_changed_at,
            "request_ids": request_ids,
            "pinned": self.pinned,
            "busy": self.busy,
            "num_replicas": len(self.replicas),
            "replicas": [r.get_state() for r in self.replicas.values()],
        }

    async def kill_request(self, request_id: str) -> dict:
        """Kill a specific request by ID.

        Searches in-flight requests on every replica first, then the queue.
        """
        # In-flight on a replica?
        for replica in list(self.replicas.values()):
            if replica.current_request is None:
                continue
            if replica.current_request.id != request_id:
                continue
            cancelled = await replica.cancel_current_request()
            if cancelled:
                return {
                    "status": "cancelled_execution",
                    "message": (
                        f"Cancelled executing request {request_id} on replica "
                        f"{replica.replica_id}"
                    ),
                }
            return {
                "status": "error",
                "message": (
                    f"Error cancelling request {request_id} on replica "
                    f"{replica.replica_id}"
                ),
            }

        # Queued?
        found = None
        for request in self.queue._queue:
            if request.id == request_id:
                found = request
                break

        if found is not None:
            self.queue._queue.remove(found)

            await found.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                "Request cancelled.",
            ).arespond()

            await self.reply()

            return {
                "status": "removed_from_queue",
                "message": f"Removed request {request_id} from queue",
            }

        return {
            "status": "not_found",
            "message": f"Request {request_id} not found in processor {self.model_key}",
        }
