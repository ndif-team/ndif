"""Replica abstraction for the per-model Processor pool.

A ``Replica`` wraps a single ModelActor instance: it owns the actor handle
(once ready), the worker asyncio.Task pulling requests from a shared queue,
the in-flight request tracking, and the per-request dispatch + drift
detection. The Processor holds a ``Dict[REPLICA_ID, Replica]`` and is
otherwise hands-off — the Replica drives its own lifecycle and signals the
Processor through callbacks set up at ``start`` time.

Replicas are tightly scoped: they don't decide whether the Processor should
be torn down when they exit (the Processor does that, based on the
``on_exit`` callback firing for the *last* live Replica). They also don't
own the request queue itself — multiple Replicas of the same model share
one queue, which is the load-balancing mechanism.
"""

import asyncio
import logging
import time
from typing import Any, Callable, Optional

from opentelemetry import trace
from ray.exceptions import ActorDiedError

from ....common.providers.ray import (
    CachedActorError,
    controller_handle,
    get_model_actor_handle,
)
from ....common.schema.controller import DeployResponse, ModelDeployResult
from ....common.schema.deployment_config import DeploymentConfig
from ....common.schema.request import BackendRequestModel
from ....common.schema.response import BackendResponseModel
from ....common.tracing import (
    TracingContext,
    set_request_attributes,
    trace_span,
)
from ....common.types import MODEL_KEY, REPLICA_ID

logger = logging.getLogger("ndif")

# Exceptions that all mean "this replica is no longer serving and should be
# treated as evicted", detected by type rather than by message string:
#   - ValueError       : ``ray.get_actor`` could not find the actor (the
#                        controller evicted it, or hasn't created it yet).
#   - ActorDiedError   : the actor process died / was killed.
#   - CachedActorError : the actor is alive but moved to CPU cache (WARM);
#                        arrives wrapped in RayTaskError but isinstance holds.
EVICTED_ERRORS = (ValueError, ActorDiedError, CachedActorError)


class Replica:
    """A single ModelActor replica serving requests for one ``model_key``.

    Attributes:
        model_key: The model this replica serves.
        replica_id: 5-char identifier, unique across the cluster.
        handle: Ray ActorHandle once ``__ray_ready__`` has resolved; ``None``
            before setup completes and after the worker exits.
        current_request: The request currently in flight, or ``None``.
        current_started_at: Unix timestamp when ``current_request`` started,
            or ``None``.
    """

    def __init__(self, model_key: MODEL_KEY, replica_id: REPLICA_ID) -> None:
        self.model_key = model_key
        self.replica_id = replica_id
        self.handle: Optional[Any] = None
        self.current_request: Optional[BackendRequestModel] = None
        self.current_started_at: Optional[float] = None
        self.task: Optional[asyncio.Task] = None
        # Flipped to True by ``cancel`` or by drift detection in dispatch.
        # Causes the worker loop to exit on the next iteration.
        self.dropped: bool = False

    @property
    def ready(self) -> bool:
        """Whether this replica can accept a request right now."""
        return self.handle is not None and not self.dropped

    @property
    def busy(self) -> bool:
        """Whether the replica is currently executing a request."""
        return self.current_request is not None

    @classmethod
    async def deploy(cls, model_key: MODEL_KEY, replicas: int = 1) -> ModelDeployResult:
        """Ask the controller to deploy ``replicas`` new replicas of ``model_key``.

        ``config.replicas`` is additive on the controller side, so this is
        purely "add N more"; existing replicas are not counted toward the
        request. The controller guarantees the returned ``ModelDeployResult``
        has either ``replicas`` populated or ``error`` set — callers only
        need to check ``result.error``.
        """
        deploy_response: DeployResponse = await controller_handle().deploy.remote(
            {model_key: DeploymentConfig(replicas=replicas)},
            trace_context=TracingContext.inject(),
        )

        return deploy_response.results[model_key]

    def start(
        self,
        queue: asyncio.Queue,
        error_queue: asyncio.Queue,
        ready_event: asyncio.Event,
        on_exit: Callable[["Replica"], None],
    ) -> None:
        """Spawn the worker task.

        Args:
            queue: Shared per-model request queue. The worker pulls from
                this once setup completes.
            error_queue: Shared error sink (for non-drift errors, e.g.
                connection failures that the dispatcher must observe).
            ready_event: Set by this Replica when ``__ray_ready__`` resolves.
                Lets the Processor block on "at least one replica ready"
                without polling. Also set in ``finally`` so a stuck Processor
                doesn't deadlock if every replica fails setup.
            on_exit: Fired synchronously from the worker's ``finally`` block
                once the Replica is fully torn down. The Processor uses this
                to drop the Replica from its pool and check for last-replica
                exit.
        """
        self.task = asyncio.create_task(
            self.replica_worker(queue, error_queue, ready_event, on_exit)
        )

    async def cancel(self, message: Optional[str] = None) -> None:
        """Cancel the worker task.

        Idempotent. ``on_exit`` fires from the worker's ``finally`` block
        regardless of how it exits, so the Processor will see the removal.

        If a request is in flight, error it here too. A purge/critical-error
        teardown only notifies *queued* requests via ``Processor.reply``; the
        running request would otherwise be left hanging until (or unless) the
        cancellation propagates through ``dispatch``. We grab it before
        cancelling the task and send the ERROR response directly.
        """
        self.dropped = True

        request = self.current_request
        if request is not None:
            await request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                message
                or "Replica was evicted while processing your request. "
                "Sorry for the inconvenience. Please try again later.",
            ).arespond()

        if self.task is not None and not self.task.done():
            self.task.cancel()

    async def cancel_current_request(self) -> bool:
        """Tell the actor to cancel its in-flight request.

        Returns True if a cancel was issued, False if the replica isn't
        ready or wasn't running anything. Exceptions from the actor are
        logged and counted as a failed cancel.
        """
        if self.handle is None or self.current_request is None:
            return False
        try:
            await self.handle.cancel.remote()
            return True
        except Exception:
            logger.exception(
                f"Error cancelling current request on "
                f"{self.model_key}[{self.replica_id}]"
            )
            return False

    def get_state(self) -> dict:
        return {
            "replica_id": self.replica_id,
            "ready": self.ready,
            "busy": self.busy,
            "current_request_id": (
                self.current_request.id if self.current_request else None
            ),
            "current_request_started_at": self.current_started_at,
        }

    async def replica_worker(
        self,
        queue: asyncio.Queue,
        error_queue: asyncio.Queue,
        ready_event: asyncio.Event,
        on_exit: Callable[["Replica"], None],
    ) -> None:
        """Main per-replica task: setup → loop → cleanup.

        Setup waits for the actor to come up (polling through
        ``await_ready``). Once ready, the loop pulls requests from the
        shared queue and dispatches them. Drift detection inside
        ``dispatch`` flips ``self.dropped``, exiting the loop. On any exit
        path the ``finally`` block clears local state, wakes the
        ``ready_event`` (so a Processor still in DEPLOYING doesn't hang),
        and fires ``on_exit`` so the Processor can drop us from its pool.
        """
        try:
            handle = await self.await_ready(error_queue)
            if handle is None:
                return

            self.handle = handle
            ready_event.set()

            while self.ready:
                request = await queue.get()
                if not self.ready:
                    # We were dropped while waiting; hand the request back
                    # so another replica can pick it up.
                    queue.put_nowait(request)
                    return
                await self.dispatch(request, error_queue)

        except asyncio.CancelledError:
            return

        finally:
            self.handle = None
            self.current_request = None
            self.current_started_at = None
            # Wake any Processor still waiting on the ready_event so it can
            # observe cancellation rather than hanging when every replica
            # bombs out at setup.
            ready_event.set()
            on_exit(self)

    async def await_ready(self, error_queue: asyncio.Queue) -> Optional[Any]:
        """Poll until the actor exists, then await ``__ray_ready__``.

        Returns the ActorHandle on success, or ``None`` if setup failed
        unrecoverably (or the replica was cancelled mid-poll).

        Non-drift exceptions (i.e. anything other than a ``ValueError`` from
        ``ray.get_actor`` failing to find the actor) are surfaced via
        ``error_queue`` so the dispatcher can observe them — most importantly
        so a Ray connection error during setup triggers the dispatcher's
        reconnect path instead of being silently swallowed.
        """
        with trace_span(
            "replica.await_ready",
            attributes={
                "ndif.model.key": self.model_key,
                "ndif.replica.id": self.replica_id,
            },
        ) as span:
            while not self.dropped:
                try:
                    handle = get_model_actor_handle(self.model_key, self.replica_id)
                    await handle.__ray_ready__.remote()
                    return handle
                except ValueError:
                    # ``ray.get_actor`` raises ValueError when the actor isn't
                    # found. Controller.apply() creates the actor asynchronously
                    # after deploy() returns — keep waiting.
                    await asyncio.sleep(1)
                    continue
                except Exception as e:
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    span.record_exception(e)
                    logger.error(
                        f"Replica {self.model_key}[{self.replica_id}] "
                        f"failed to come up: {e}"
                    )
                    error_queue.put_nowait((self.model_key, e))
                    return None
            return None

    async def dispatch(
        self,
        request: BackendRequestModel,
        error_queue: asyncio.Queue,
    ) -> None:
        """Send one request to the actor and surface any failures.

        Any of ``EVICTED_ERRORS`` (lookup ValueError, ActorDiedError, or
        CachedActorError) flips ``self.dropped`` so the worker loop exits —
        that's the drift-recovery path that handles "the controller
        evicted/cached this replica, or the actor died, and we didn't know".
        Other exceptions are reported via ``error_queue`` (which the
        dispatcher uses for connection-error detection) and the worker keeps
        serving.
        """
        parent_ctx = TracingContext.extract(request.trace_context)

        with trace_span("replica.dispatch", parent_context=parent_ctx) as span:
            set_request_attributes(span, request)
            span.set_attribute("ndif.replica.id", self.replica_id)

            self.current_request = request
            self.current_started_at = time.time()

            try:
                handle = self.handle
                if handle is None:
                    await request.create_response(
                        BackendResponseModel.JobStatus.ERROR,
                        logger,
                        "Replica evicted before dispatch. Sorry for the inconvenience. Please try again later.",
                    ).arespond()
                    return

                await request.create_response(
                    BackendResponseModel.JobStatus.DISPATCHED,
                    logger,
                    "Your job has been sent to the model deployment.",
                ).arespond()

                span.add_event("dispatched_to_model_actor")

                # Re-inject so the Ray actor sees the current span as parent.
                request.trace_context = TracingContext.inject()

                await handle.__call__.remote(request)

            except asyncio.CancelledError:
                # The worker task was cancelled mid-dispatch (almost always
                # because Processor.reconcile saw our replica disappear from
                # the controller and called replica.cancel(), e.g. after a
                # user-initiated evict). CancelledError inherits from
                # BaseException so the broader ``except Exception`` below
                # would NOT have caught it — without this branch the user
                # is left stuck on the DISPATCHED status forever. Surface
                # an explicit error to the user, then re-raise so the
                # worker still exits cleanly.
                span.set_status(trace.StatusCode.ERROR, "cancelled")
                await request.create_response(
                    BackendResponseModel.JobStatus.ERROR,
                    logger,
                    "Replica was evicted while processing your request. Sorry for the inconvenience. Please try again later.",
                ).arespond()
                raise

            except EVICTED_ERRORS as e:
                # The replica is gone: the controller evicted it (lookup
                # ValueError), the actor died/was killed (ActorDiedError), or
                # it was moved to CPU cache (CachedActorError). All mean the
                # same thing for us — drop from the pool so the worker loop
                # exits and let the request be retried elsewhere.
                span.set_status(trace.StatusCode.ERROR, str(e))
                span.record_exception(e)

                await request.create_response(
                    BackendResponseModel.JobStatus.ERROR,
                    logger,
                    "Replica was evicted. Sorry for the inconvenience. "
                    "Please try again later.",
                ).arespond()

                logger.warning(
                    f"Replica {self.model_key}[{self.replica_id}] evicted "
                    f"({type(e).__name__}) — dropping from pool."
                )
                self.dropped = True

            except Exception as e:
                span.set_status(trace.StatusCode.ERROR, str(e))
                span.record_exception(e)

                await request.create_response(
                    BackendResponseModel.JobStatus.ERROR,
                    logger,
                    "Error submitting request to model deployment. "
                    "Please try again later. Sorry for the inconvenience.",
                ).arespond()

                error_queue.put_nowait((self.model_key, e))

            finally:
                self.current_request = None
                self.current_started_at = None
