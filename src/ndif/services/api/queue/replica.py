"""Replica abstraction for the per-model Processor pool.

A ``Replica`` wraps a single model actor: it owns the worker asyncio.Task that
pulls from the shared per-model queue and dispatches each request to the actor.
It holds a back-reference to its ``Processor`` and drives its own membership —
when the worker task ends it removes itself from the Processor's pool and asks
the Processor to re-provision if work remains.

A Replica is "dropped" once its worker task is done (or never started); there is
no separate flag to keep in sync. The two ways a worker ends are:

  - an eviction (the actor was reclaimed / moved to CPU cache out from under us),
    in which case the in-flight request is handed back to the *front* of the
    Processor's queue rather than errored, so a sibling replica (or a freshly
    re-provisioned one) can pick it up; and
  - a deliberate ``cancel`` (operator kill, reconcile, or a connection-error
    purge), which errors the in-flight request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from ray.exceptions import ActorDiedError

from ....common.providers.ray import (
    CachedActorError,
    controller_handle,
    get_model_actor_handle,
)
from ....common.schema import Status
from ....common.schema.controller import DeployResponse, DeploymentConfig
from ....common.schema.request import BackendRequestModel
from ....common.telemetry import elapsed_ms, error_type_name, event
from ....common.types import MODEL_KEY, REPLICA_ID

if TYPE_CHECKING:
    from .processor import Processor

logger = logging.getLogger("ndif.queue.replica")


# Exceptions that all mean "this replica is no longer serving; treat as
# evicted", detected by type rather than message string:
#   - ValueError       : actor lookup failed (controller evicted it, or hasn't
#                        created it yet).
#   - ActorDiedError   : the actor process died / was killed.
#   - CachedActorError : actor alive but moved to CPU cache (WARM).
EVICTED_ERRORS = (ValueError, ActorDiedError, CachedActorError)


def is_evicted_error(error: BaseException) -> bool:
    """Whether a dispatch failure means the replica is no longer serving.

    Not a bare ``isinstance``: an exception raised *inside* the actor comes back
    wrapped in a ``ray.exceptions.RayTaskError``, and the dual
    RayTaskError-plus-cause class that would satisfy ``isinstance`` is only
    built when ``as_instanceof_cause()`` is applied. Over Ray Client — which is
    how the dispatcher connects — it is not, so the wrapper arrives as a plain
    ``RayTaskError`` and the cause has to be read off ``.cause``.

    Missing this meant a HOT->WARM demotion never re-queued: the actor raised
    CachedActorError exactly as designed, the match failed, and the request fell
    through to the generic handler and was errored to the user.
    """
    if isinstance(error, EVICTED_ERRORS):
        return True
    cause = getattr(error, "cause", None)
    return cause is not None and isinstance(cause, EVICTED_ERRORS)


class DeploymentError(Exception):
    """A deploy the controller refused, carrying the reason it gave.

    Distinct from an arbitrary failure during provisioning so the queue can tell
    "the cluster explained why this cannot be deployed" from "something broke on
    the way there". The former is safe — and useful — to show the caller: these
    messages say things like `CANT_ACCOMMODATE: placed 0 of 1 new replicas` or
    HuggingFace's own "Repository Not Found ... make sure you specified the
    correct `repo_id`". The latter is an internal fault the caller can do
    nothing with, and keeps the generic message.
    """


class Replica:
    """A single model-actor replica serving requests for one ``model_key``."""

    def __init__(
        self,
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
        processor: "Processor",
    ) -> None:
        """Initialize a Replica.

        Args:
            model_key: The model this Replica serves.
            replica_id: The controller-assigned id of the backing actor.
            processor: The owning Processor; the Replica reads its shared queue /
                error queue and drives its own pool membership through it.
        """
        self.model_key = model_key
        self.replica_id = replica_id
        self.processor = processor
        self.queue = processor.queue
        self.error_queue = processor.error_queue

        self.task: Optional[asyncio.Task] = None

        self.current_request: Optional[BackendRequestModel] = None
        self.current_started_at: Optional[float] = None

    @property
    def dropped(self) -> bool:
        """Whether this replica is no longer serving (worker done / unstarted)."""
        return self.task is None or self.task.done()

    @property
    def busy(self) -> bool:
        """Whether the replica is currently executing a request."""
        return self.current_request is not None

    @classmethod
    async def provision(cls, model_key: MODEL_KEY, processor: "Processor") -> "Replica":
        """Ask the controller to deploy one replica and wrap it.

        ``DeploymentConfig.replicas`` is additive controller-side, so this adds
        one more replica for ``model_key`` and returns a Replica bound to its
        id. Raises if the controller reports a deploy error; the actor itself
        may not exist yet (``wait`` polls for it).
        """
        response: DeployResponse = await controller_handle().deploy.remote(
            {model_key: DeploymentConfig(replicas=1, trusted=processor.trusted)}
        )

        result = response.results[model_key]
        if result.error:
            raise DeploymentError(result.error)

        return cls(model_key, result.replicas[0], processor)

    @classmethod
    async def deploy(cls, model_key: MODEL_KEY, processor: "Processor") -> "Replica":
        """Provision a fresh replica, register it, wait for ready, and start it.

        Registers with the Processor *before* the worker starts so it counts
        against the pool (autoscaling / dedupe) while coming up; on a setup
        failure it de-registers before propagating.
        """
        replica = await cls.provision(model_key, processor)
        processor.replicas[replica.replica_id] = replica
        try:
            await replica.wait()
        except Exception:
            processor.replicas.pop(replica.replica_id, None)
            raise
        replica.start()
        return replica

    async def wait(self) -> None:
        """Block until the backing actor exists and is ready to serve.

        The controller creates the actor asynchronously after ``deploy``
        returns, so a lookup ``ValueError`` just means "not yet" — poll until it
        appears. Any other exception (most importantly a connection error)
        propagates to the caller, which reports it.
        """
        while True:
            try:
                handle = get_model_actor_handle(self.model_key, self.replica_id)
                await handle.__ray_ready__.remote()
                return
            except ValueError:
                await asyncio.sleep(1)
                continue

    def start(self) -> None:
        """Spawn the worker task pulling from the shared queue."""
        self.task = asyncio.create_task(self.worker())

    async def worker(self) -> None:
        """Pull requests and dispatch them until dropped.

        On an eviction ``dispatch`` sets ``self.task = None`` (having handed the
        in-flight request back to the front of the queue), so the loop condition
        flips and the worker falls out. However it exits, the ``finally`` drops
        this replica from the pool and asks the Processor to re-provision if
        requests are still waiting.
        """
        try:
            while not self.dropped:
                request = await self.queue.get()
                await self.dispatch(request)
        finally:
            self.processor.replicas.pop(self.replica_id, None)
            if not self.processor.queue.empty():
                event(
                    logger,
                    f"Replica {self.model_key}[{self.replica_id}] exited with "
                    f"{self.processor.queue.qsize()} request(s) queued; "
                    f"ensuring a replica.",
                    level=logging.WARNING,
                    model_key=self.model_key,
                    replica_id=self.replica_id,
                    queue_size=self.processor.queue.qsize(),
                    event_type="reprovision",
                )
                self.processor.ensure_started()
            else:
                self.processor.mark_idle()

    async def dispatch(self, request: BackendRequestModel) -> None:
        """Send one request to the actor and surface any failures.

        On an eviction the in-flight request is handed back to the *front* of
        the Processor's queue (``enqueue(prepend=True)``) and this replica is
        dropped (``self.task = None``) so the worker loop exits. A
        ``CancelledError`` (deliberate cancel) errors the request and re-raises.
        Other exceptions error the request and are reported via ``error_queue``
        for connection-error detection, and the worker keeps serving.
        """
        self.current_request = request
        self.current_started_at = time.time()

        try:
            handle = get_model_actor_handle(self.model_key, self.replica_id)

            await request.arespond(
                Status.DISPATCHED,
                "Your job has been sent to the model deployment.",
            )

            await handle.run.remote(request)

        except asyncio.CancelledError:
            # The worker task was cancelled mid-dispatch (operator kill,
            # reconcile, or a connection-error purge). CancelledError inherits
            # BaseException, so the ``except Exception`` below would NOT catch
            # it — without this branch the user is stuck on DISPATCHED forever.
            # Surface an error, then re-raise to exit.
            await request.arespond(
                Status.ERROR,
                "Replica was evicted while processing your request. Sorry for "
                "the inconvenience. Please try again later.",
            )
            event(
                logger,
                "request errored: cancelled mid-dispatch",
                level=logging.WARNING,
                model_key=self.model_key,
                replica_id=self.replica_id,
                request_id=request.id,
                api_key=request.api_key,
                email=request.email,
                stage="running",
                error_type="cancelled",
                exec_ms=elapsed_ms(self.current_started_at),
            )
            raise

        except Exception as e:
            if is_evicted_error(e):
                # The replica is gone (lookup failure, actor died, or moved to
                # CPU cache). Drop this replica (task -> None ends the worker
                # loop) and hand the in-flight request back to the front of the
                # queue for a sibling or a freshly re-provisioned replica.
                self.task = None
                await self.processor.enqueue(request, prepend=True)
                event(
                    logger,
                    f"Replica {self.model_key}[{self.replica_id}] evicted "
                    f"({type(e).__name__}) — re-queueing request, dropping from pool.",
                    level=logging.WARNING,
                    model_key=self.model_key,
                    replica_id=self.replica_id,
                    request_id=request.id,
                    api_key=request.api_key,
                    email=request.email,
                    stage="running",
                    error_type=f"evicted:{error_type_name(e)}",
                    exec_ms=elapsed_ms(self.current_started_at),
                )
                return
            await request.arespond(
                Status.ERROR,
                "Error submitting request to model deployment. "
                "Please try again later. Sorry for the inconvenience.",
            )
            event(
                logger,
                "request errored during execution",
                level=logging.ERROR,
                exc_info=True,
                model_key=self.model_key,
                replica_id=self.replica_id,
                request_id=request.id,
                api_key=request.api_key,
                email=request.email,
                stage="running",
                error_type=error_type_name(e),
                exec_ms=elapsed_ms(self.current_started_at),
            )
            self.error_queue.put_nowait((self.model_key, e))

        finally:
            self.current_request = None
            self.current_started_at = None

    async def cancel(self, message: Optional[str] = None) -> None:
        """Cancel the worker task, erroring any in-flight request.

        Idempotent. The worker's ``finally`` runs even under cancellation, so
        the Processor still sees this replica drop itself. If a request is in
        flight, error it here — a teardown only notifies *queued* requests via
        ``Processor.reply``; the running one would otherwise hang.
        """
        request = self.current_request
        if request is not None:
            await request.arespond(
                Status.ERROR,
                message
                or "Replica was evicted while processing your request. "
                "Sorry for the inconvenience. Please try again later.",
            )

        if self.task is not None and not self.task.done():
            self.task.cancel()
