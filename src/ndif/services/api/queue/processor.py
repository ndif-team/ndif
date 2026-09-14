"""Processor: per-model request queue, replica pool, and dispatch.

Each Processor handles requests for exactly one ``model_key``, holding a request
queue and a pool of :class:`Replica` workers that share it.

The Processor is lazy and self-healing:

  - ``enqueue`` puts the request on the queue and, if there are no live
    replicas, kicks off ``start`` to provision one.
  - ``start`` discovers existing replicas (or deploys a fresh one), waits for
    them to be ready, and spawns their workers.
  - When a replica's worker exits (eviction or cancel), the Replica removes
    itself from the pool and — if requests are still queued — asks the Processor
    (``ensure_started``) to re-provision, so an eviction re-queues the in-flight
    request rather than dropping it.
  - A single long-lived ``autoscaling_loop`` adds more replicas under sustained
    queue pressure.

There is no teardown: an idle Processor simply sits with an empty pool and is
re-used (re-provisioned) on the next request.
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Dict, Optional

from ....common.providers.ray import controller_handle
from ....common.schema import Status
from ....common.schema.controller import ReplicaStates
from ....common.schema.request import BackendRequestModel
from ....common.telemetry import error_type_name, event
from ....common.types import MODEL_KEY, REPLICA_ID
from .config import CONFIG
from .replica import DeploymentError, Replica
from .request_queue import RequestQueue

logger = logging.getLogger("ndif.queue.processor")

# Cap on a controller-supplied failure reason forwarded to the caller. The
# useful ones (HuggingFace's "Repository Not Found ...", CANT_ACCOMMODATE) are
# well under this; the cap only stops a pathological error string becoming the
# websocket payload.
_MAX_REASON_CHARS = 1500


class ProcessorStatus(Enum):
    """Processor lifecycle states.

    UNINITIALIZED / READY are steady states (idle pool vs. serving); PROVISIONING
    and DEPLOYING are the transient setup phases. Replica-level busy state lives
    on each :class:`Replica`, so there's no global BUSY here — the Processor is
    READY whenever at least one replica is serving.
    """

    UNINITIALIZED = "uninitialized"
    PROVISIONING = "provisioning"
    DEPLOYING = "deploying"
    READY = "ready"
    CANCELLED = "cancelled"


class Processor:
    """Orchestrates per-model request queue, replicas, and dispatch."""

    def __init__(
        self,
        model_key: MODEL_KEY,
        error_queue: asyncio.Queue,
    ) -> None:
        """Initialize a Processor for one model.

        Args:
            model_key: The model this Processor serves.
            error_queue: Shared queue for reporting errors to the dispatcher, as
                ``(model_key, exception)`` tuples; a connection error there
                triggers a Ray reconnect.
        """
        self.model_key = model_key
        self.error_queue = error_queue

        self.queue = RequestQueue()
        self.status = ProcessorStatus.UNINITIALIZED
        self.replicas: Dict[REPLICA_ID, Replica] = {}
        # Whether this model's deployment loads with trust_remote_code; set from the
        # request that kicks off provisioning (see ensure_started) and threaded into
        # the deploy config.
        self.trusted = False

        self.autoscaling_task: asyncio.Task[None] = asyncio.create_task(
            self.autoscaling_loop()
        )

    @property
    def busy(self) -> bool:
        """Whether any replica is currently executing a request."""
        return any(replica.busy for replica in self.replicas.values())

    async def enqueue(
        self, request: BackendRequestModel, prepend: bool = False
    ) -> None:
        """Add a request to the queue, provision if needed, acknowledge it.

        ``prepend`` puts the request at the front of *its own priority group*,
        not of the whole queue — used when an evicted replica hands its in-flight
        request back, so it keeps its place in line. A priority request does
        **not** prepend: it sorts ahead of normal traffic by group, and stays
        FIFO against its peers. Prepending it instead made the priority group
        LIFO, which starved everyone (see ``request_queue``).

        A re-queued request keeps its original ``enqueued_at``, so the autoscaler
        still sees how long it has really waited.
        """
        # RequestQueue stamps enqueued_at if unset and orders by
        # (group, prepend, enqueued_at) -- see request_queue.py.
        self.queue.put(request, prepend=prepend)

        self.ensure_started(request.trusted)

        # Queue-depth / throughput signal: position is 1-based depth at enqueue.
        event(
            logger,
            "request enqueued",
            model_key=self.model_key,
            request_id=request.id,
            api_key=request.api_key,
            email=request.email,
            stage="queued",
            queue_size=self.queue.qsize(),
            replicas=len(self.replicas),
            processor_status=self.status.value,
        )

        # Ask the queue where the request actually landed. Depth-at-enqueue was
        # only ever right for a plain append: a priority or re-queued request
        # sorts ahead of others, and telling such a caller they were last in a
        # line they had just jumped is exactly backwards.
        position = self.queue.position(request.id) or self.queue.qsize()

        await self.reply(
            request=request,
            description=(
                f"Added to Queue at position {position}."
                if self.status
                not in (ProcessorStatus.PROVISIONING, ProcessorStatus.DEPLOYING)
                else None
            ),
        )

    def ensure_started(self, trusted: Optional[bool] = None) -> None:
        """Kick off provisioning when the pool is empty and idle.

        No-ops if a replica is already live or setup is already underway, so
        it's safe to call on every enqueue and on last-replica exit. ``trusted``
        (the kicking-off request's flag) sets whether the deployment loads with
        trust_remote_code; a re-provision passes ``None`` to keep the value from the
        request that first deployed it.
        """
        if self.replicas:
            return
        if self.status in (ProcessorStatus.PROVISIONING, ProcessorStatus.DEPLOYING):
            return
        if trusted is not None:
            self.trusted = trusted
        self.status = ProcessorStatus.PROVISIONING
        asyncio.create_task(self.start())

    def mark_idle(self) -> None:
        """Drop back to UNINITIALIZED once the pool has drained to nothing.

        Called by the last replica's worker as it exits with an empty queue, so
        ``status`` reflects "idle, nothing to serve" rather than a stale READY.
        No-ops while any replica remains.
        """
        if not self.replicas:
            self.status = ProcessorStatus.UNINITIALIZED

    async def start(self) -> None:
        """Bring the pool up: discover or deploy, wait ready, spawn workers.

        Queries the controller: if it already lists replicas, adopt all of them;
        otherwise deploy a fresh one. Each is registered, waited on, and given a
        worker. On any failure (including a connection error) the phase-specific
        ``error_message`` is logged with a traceback, reported to the error queue
        (so the dispatcher reconnects on a connection error), and passed to
        ``purge`` — which errors the queued users and clears the pool so nothing
        hangs.
        """
        error_message = (
            "Error starting model. Please try again later. Sorry for the inconvenience."
        )

        try:
            response: ReplicaStates = await controller_handle().get_deployment.remote(
                self.model_key
            )

            if response.replicas:
                for state in response.replicas:
                    replica = Replica(self.model_key, state.replica_id, self)
                    self.replicas[replica.replica_id] = replica
                self.status = ProcessorStatus.DEPLOYING
            else:
                error_message = (
                    "Error provisioning model. "
                    "Please try again later. Sorry for the inconvenience."
                )
                replica = await Replica.provision(self.model_key, self)
                self.replicas[replica.replica_id] = replica
                self.status = ProcessorStatus.DEPLOYING

            error_message = (
                "Error starting model. "
                "Please try again later. Sorry for the inconvenience."
            )
            await self.reply()

            for replica in list(self.replicas.values()):
                await replica.wait()
                replica.start()
                self.status = ProcessorStatus.READY
            await self.reply()

        except Exception as e:
            event(
                logger,
                error_message,
                level=logging.ERROR,
                exc_info=True,
                model_key=self.model_key,
                stage=self.status.value,
                error_type=error_type_name(e),
            )
            self.error_queue.put_nowait((self.model_key, e))

            # When the controller said *why* it refused, tell the caller that
            # instead of the canned line. These reasons are the actionable ones
            # -- a mistyped repo, a gated repo, a model too big for the cluster
            # -- and HuggingFace phrases most of them for end users already,
            # naming the repo and the page to visit. They were being logged and
            # then replaced with "Please try again later", which is advice that
            # cannot work for any of them.
            #
            # Deliberately only DeploymentError: any other exception here is an
            # internal fault whose text would leak implementation detail and
            # tell the caller nothing.
            if isinstance(e, DeploymentError):
                reason = str(e).strip()
                if len(reason) > _MAX_REASON_CHARS:
                    reason = reason[:_MAX_REASON_CHARS].rstrip() + "…"
                user_message = f"Could not deploy this model. {reason}"
            else:
                user_message = error_message

            if self.status != ProcessorStatus.READY:
                await self.purge(user_message)

    async def autoscaling_loop(self) -> None:
        """Scale the replica pool up under sustained queue pressure.

        A single long-lived task (created in ``__init__``). Every
        ``autoscaling_interval_s`` seconds, while serving, find the request that
        has waited longest across both priority groups. If it has waited longer
        than ``autoscaling_wait_threshold_s``, ask for one more replica and
        sleep ``autoscaling_backoff_s`` before re-checking
        (so the new replica can drain depth before another scale-up). Only acts
        when READY — first-replica provisioning is ``start``'s job — and stops
        once ``autoscaling_max_replicas`` replicas are running.

        Each tick is guarded so a transient error can't kill the task and leave
        the Processor unable to scale for the rest of its life.
        """
        while self.status != ProcessorStatus.CANCELLED:
            try:
                if self.status == ProcessorStatus.READY:
                
                    oldest = self.queue.oldest()
                    if oldest is not None and oldest.enqueued_at is not None:
                        wait = time.time() - oldest.enqueued_at
                        if (
                            wait > CONFIG.autoscaling_wait_threshold_s
                            and len(self.replicas) < CONFIG.autoscaling_max_replicas
                        ):
                            await self.scale_up(wait)
                            await asyncio.sleep(CONFIG.autoscaling_backoff_s)
                            continue

                await asyncio.sleep(CONFIG.autoscaling_interval_s)
            except Exception:
                logger.exception(
                    f"autoscaling loop error for {self.model_key}; continuing"
                )
                await asyncio.sleep(CONFIG.autoscaling_interval_s)

    async def scale_up(self, wait: float) -> None:
        """Deploy one more replica and bring it up.

        ``Replica.deploy`` registers the replica before its worker starts (so it
        counts against ``autoscaling_max_replicas`` while coming up), then waits
        for readiness — so this blocks the autoscaling loop until the new replica
        is ready, which is fine since the loop backs off after a scale-up anyway.
        """
        replicas_before = len(self.replicas)
        logger.info(
            f"Autoscaling {self.model_key}: oldest queued request has waited "
            f"{wait:.1f}s "
            f"(>{CONFIG.autoscaling_wait_threshold_s}s); adding a replica.",
            extra={
                "model_key": self.model_key,
                "event": "autoscale_trigger",
                "wait_s": round(wait, 2),
                "threshold_s": CONFIG.autoscaling_wait_threshold_s,
                "queue_size": self.queue.qsize(),
                "replicas_before": replicas_before,
            },
        )

        try:
            # ``deploy`` registers the replica with this Processor before its
            # worker starts, so it counts toward the pool while coming up.
            await Replica.deploy(self.model_key, self)
        except Exception as e:
            self.error_queue.put_nowait((self.model_key, e))
            event(
                logger,
                f"Autoscaling {self.model_key} failed to add replica: {e}",
                level=logging.WARNING,
                exc_info=True,
                model_key=self.model_key,
                autoscale_result="error",
                replicas_before=replicas_before,
                error=str(e),
            )
            return

        event(
            logger,
            f"Autoscaling {self.model_key}: provisioning replica; "
            f"{replicas_before} -> {len(self.replicas)}",
            model_key=self.model_key,
            autoscale_result="added",
            replicas_before=replicas_before,
            replicas_after=len(self.replicas),
        )

    async def reply(
        self,
        request: Optional[BackendRequestModel] = None,
        description: Optional[str] = None,
        status: Status = Status.QUEUED,
    ) -> None:
        """Send a status message to queued users.

        ``request=None`` broadcasts to every queued request, annotating each
        with its position. Otherwise the message goes to the single request.
        A ``None`` description is resolved to the current setup phase's text.
        """
        if description is None:
            if self.status == ProcessorStatus.PROVISIONING:
                description = "Model Provisioning..."
                status = Status.PROVISIONING
            elif self.status == ProcessorStatus.DEPLOYING:
                description = "Model Deploying..."
                status = Status.DEPLOYING

        if request is None:
            for i, queued in enumerate(self.queue.snapshot()):
                await queued.arespond(
                    status,
                    description
                    if description is not None
                    else f"Moved to position {i + 1} in Queue.",
                )
        else:
            await request.arespond(status, description or "")

    async def reconcile(self) -> None:
        """Match the pool to the controller's replica list, both directions.

        Invoked after an out-of-band deploy/evict (via the dispatcher's events
        worker):

          - replicas the controller has dropped are *left alone* — see below;
          - replicas the controller has gained are adopted, so capacity added
            out-of-band (``ndif deploy``, the dashboard) is actually used.

        Adoption matters because it is the *only* path that picks up a new
        replica while the model is already serving: ``ensure_started`` no-ops
        whenever the pool is non-empty, and ``start`` only runs on an empty
        pool. Without it, deploying a second replica of a busy model added no
        capacity at all until the dispatcher restarted.

        Shedding, by contrast, is deliberately **not** done here. It used to
        call ``Replica.cancel``, which errors the in-flight request — so an
        eviction that demoted a replica to WARM killed the request running on
        it, even though that request was blameless and still runnable. The
        worker already handles this correctly on its own: the next (or current)
        dispatch to a gone replica raises one of ``EVICTED_ERRORS``
        (``CachedActorError`` when demoted, ``ValueError``/``ActorDiedError``
        when removed), which hands the request back to the *front* of the queue,
        drops the replica, and re-provisions. Letting that happen is both
        simpler and the only version that doesn't lose work.

        The cost is that a replica whose worker is idle lingers in the pool
        until traffic exercises it. That is harmless — there is nothing to serve
        while the queue is empty — and self-corrects on the next request, which
        pays one wasted dispatch and is then re-queued and served.

        A connection error is reported so the dispatcher can reconnect.
        """
        try:
            response: ReplicaStates = await controller_handle().get_deployment.remote(
                self.model_key
            )
        except Exception as e:
            self.error_queue.put_nowait((self.model_key, e))
            return

        current = {state.replica_id for state in response.replicas}
        shed = set(self.replicas) - current
        if shed:
            # Logged, not cancelled: the dispatch that next touches one of these
            # re-queues its request and drops the replica by itself.
            event(
                logger,
                "replicas no longer listed by the controller; "
                "leaving them to drop themselves on next dispatch",
                model_key=self.model_key,
                replica_ids=sorted(shed),
                replicas=len(self.replicas),
            )

        # A start() already in flight adopts everything the controller lists, so
        # adopting here too would race it and leave two workers on one replica.
        if self.status in (ProcessorStatus.PROVISIONING, ProcessorStatus.DEPLOYING):
            return

        for replica_id in current - set(self.replicas):
            replica = Replica(self.model_key, replica_id, self)
            # Register before waiting so the replica counts against the pool
            # (keeping ensure_started from provisioning a duplicate) while it
            # comes up.
            self.replicas[replica_id] = replica
            # Backgrounded: this is awaited by the dispatcher's events worker,
            # and Replica.wait has no timeout — doing it inline would wedge that
            # worker (and every later reconcile/kill) on one unready actor.
            asyncio.create_task(self.adopt(replica))

    async def adopt(self, replica: Replica) -> None:
        """Wait for an out-of-band replica to be ready, then put it to work."""
        try:
            await replica.wait()
        except Exception as e:
            self.replicas.pop(replica.replica_id, None)
            event(
                logger,
                "failed to adopt replica",
                level=logging.ERROR,
                exc_info=True,
                model_key=self.model_key,
                replica_id=replica.replica_id,
                error_type=error_type_name(e),
            )
            self.error_queue.put_nowait((self.model_key, e))
            return

        replica.start()
        self.status = ProcessorStatus.READY
        event(
            logger,
            "adopted replica",
            model_key=self.model_key,
            replica_id=replica.replica_id,
            replicas=len(self.replicas),
        )
        await self.reply()

    async def purge(self, message: Optional[str] = None) -> None:
        """Error every queued request and cancel all replica workers.

        Called on a critical failure — the dispatcher on a Ray connection error,
        or ``start`` on a failed provision. The queue is cleared before replicas
        are cancelled so their exits don't re-provision against requests that
        were just errored.
        """
        if message is None:
            message = (
                "Critical server error occurred. "
                "Please try again later. Sorry for the inconvenience."
            )

        await self.reply(description=message, status=Status.ERROR)
        self.queue.clear()

        for replica in list(self.replicas.values()):
            await replica.cancel(message)

        # Drop any replicas that were registered but never started their worker
        # (so no ``finally`` will remove them); started ones self-remove too.
        self.replicas.clear()
        self.status = ProcessorStatus.UNINITIALIZED

    def snapshot(self) -> dict:
        """A JSON-serializable view of this Processor's live state (for `ndif queue`)."""
        return {
            "model_key": self.model_key,
            "status": self.status.value,
            "queue_size": self.queue.qsize(),
            "request_ids": [request.id for request in self.queue.snapshot()],
            "replicas": [
                {
                    "replica_id": replica.replica_id,
                    "ready": not replica.dropped,
                    "busy": replica.busy,
                    "current_request_id": (
                        replica.current_request.id
                        if replica.current_request
                        else None
                    ),
                    "current_started_at": replica.current_started_at,
                }
                for replica in self.replicas.values()
            ],
        }

    def pop_queued(self, request_id: str) -> Optional[BackendRequestModel]:
        """Remove a queued request by id and return it, or None if not queued."""
        return self.queue.remove(request_id)

    def executing_replica(self, request_id: str) -> Optional[Replica]:
        """The Replica currently running ``request_id``, or None."""
        for replica in self.replicas.values():
            if (
                replica.current_request is not None
                and replica.current_request.id == request_id
            ):
                return replica
        return None
