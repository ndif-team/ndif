import asyncio
import logging
import time
from typing import Any, Optional
from uuid import uuid4

from nnsight.intervention.serialization import UnknownPersistentIdError
from nnsight.schema.request import RequestModel
from pydantic import Field

from ..errors import ArchitectureMismatchError, PayloadError
from ..telemetry import event
from .response import BackendResponseModel, Status

# Object-store key holding a non-blocking request's latest status response.
def _response_key(request_id: str) -> str:
    return f"responses/{request_id}.json"

# Request lifecycle logs are emitted under this component.
logger = logging.getLogger("ndif.request")


class BackendRequestModel(RequestModel):
    """Server-side request.

    Subclasses the client's ``RequestModel`` (which carries ``model_key`` /
    ``session_id`` and the (de)serialization of the blob) and adds the
    server-side concerns: a request ``id`` and the ``api_key``.

    ``id`` is a fresh uuid minted per request — distinct from ``session_id``
    (which addresses the client's websocket) — so every request is identifiable
    on its own. It is what responses are stamped with.

    ``payload`` is the serialized interventions blob. It rides on the request
    through the queue and is handed to the model actor through Ray (it is not
    part of the client's JSON envelope). ``enqueued_at`` is stamped when the
    request joins a model's queue; the autoscaler reads it to spot a stale queue
    head. The request's ingress time isn't a separate field — it's the initial
    ``last_status_time`` (seeded at RECEIVED), and per-stage latency is recovered
    from the ``status_time`` metric rather than threaded through here.
    """

    id: str = Field(default_factory=lambda: uuid4().hex)
    api_key: Optional[str] = None
    # The owning user's email, resolved once at ingress from the api_key (auth
    # lookup) and carried with the request — including across the Ray boundary
    # via pickling — so every downstream log/metric can be attributed to a human
    # user, not just an opaque key. ``None`` when auth is off or the key has no
    # user (the "if exists" case).
    email: Optional[str] = None

    # Whether this request's code may run without sandbox isolation. When True the
    # sandbox deployment executes it in-process (the base, unsandboxed path) rather
    # than shipping it to a runner. Default False: sandbox everything. (How trust is
    # decided at ingress is set elsewhere.)
    trusted: bool = False

    # Whether this request jumps the queue. When True it's prepended to the front
    # of its model's queue at enqueue rather than appended. Default False. (Set at
    # ingress from the api key's ``priority`` tag.)
    priority: bool = False

    payload: Optional[bytes] = None
    enqueued_at: Optional[float] = None

    # Status-timing state, carried with the request (including across the Ray
    # boundary via pickling) so ``respond`` can record how long the request
    # spent in each status. ``last_status`` is the status it currently sits in;
    # ``last_status_time`` is when it entered that status. ``last_status_time`` is
    # seeded at ingress (RECEIVED), so it doubles as the request's received time.
    last_status: Optional[Status] = None
    last_status_time: Optional[float] = None

    @staticmethod
    def deserialize(*args: Any, **kwargs: Any) -> Any:
        """:meth:`RequestModel.deserialize`, with caller-facing failures.

        Every way reading a request can fail is classified here, so callers of
        deserialize need no error handling of their own and both server-side
        deserialization sites agree on what a given failure means.

        ``UnknownPersistentIdError`` is the interesting one. The block records
        each module it touches as a persistent id (``Module:<path>``) resolved
        against the server's live model, so an id with no entry means the two
        model trees were built differently — nearly always package drift, and
        usually ``transformers``, which decides a checkpoint's module layout.
        Raw, it names a module the caller never mentioned: the block references
        the whole envoy tree, so it fails at the *first* path where the trees
        diverge rather than the layer they were reaching for.

        Anything else means the bytes themselves were unreadable.
        """
        try:
            return RequestModel.deserialize(*args, **kwargs)
        except UnknownPersistentIdError as error:
            raise ArchitectureMismatchError(str(error)) from error
        except Exception as error:
            raise PayloadError(error) from error

    def response(
        self, status: Status, description: str = "", data: Optional[Any] = None
    ) -> BackendResponseModel:
        """Advance the request's lifecycle status, then build the response for it.

        :meth:`_advance_status` records the phase just left and logs the transition
        (a no-op for ``LOG`` or a repeated status). The returned
        :class:`BackendResponseModel` carries this request's id; ``data`` rides on
        COMPLETED responses to hand the client a small payload (e.g. a presigned url).
        """
        self._advance_status(status, description)
        return BackendResponseModel(
            id=self.id, status=status, description=description, data=data
        )

    def _advance_status(self, status: Status, description: str = "") -> None:
        """Advance the request's lifecycle status and emit its telemetry.

        On a genuine status change (``LOG`` updates and repeats of the current
        status are ignored), this records a :class:`RequestStatusTimeMetric`
        point for the phase just left and logs one structured lifecycle event
        for the new status, carrying that phase's duration as ``prev_stage_ms``.
        ``ERROR`` transitions log at WARNING; the phase is metered either way.
        """
        if status == Status.LOG or status == self.last_status:
            return

        from ..metrics import RequestStatusTimeMetric

        now = time.time()
        previous_status = self.last_status
        previous_time = self.last_status_time

        self.last_status = status
        self.last_status_time = now

        prev_stage_ms = None
        if previous_status is not None and previous_time is not None:
            prev_stage_ms = round((now - previous_time) * 1000, 2)
            RequestStatusTimeMetric.update(
                model_key=self.model_key,
                request_id=self.id,
                api_key=self.api_key,
                email=self.email,
                status=previous_status.value,
                duration_ms=prev_stage_ms,
            )

        event(
            logger,
            description or f"status: {status.value.lower()}",
            level=logging.WARNING if status == Status.ERROR else logging.INFO,
            model_key=self.model_key,
            request_id=self.id,
            api_key=self.api_key,
            email=self.email,
            stage=status.value.lower(),
            prev_stage=previous_status.value.lower() if previous_status else None,
            prev_stage_ms=prev_stage_ms,
        )

    def respond(
        self,
        status: Status,
        description: str = "",
        data: Optional[Any] = None,
        pickled: bool = False,
    ) -> BackendResponseModel:
        """Build a response and publish it to the client's websocket channel.

        Advances the status and builds the response (see :meth:`response`). A
        blocking request (has a ``session_id``) is published to that Redis
        channel; the API app's ``/subscribe`` websocket forwards it. A
        non-blocking request (no ``session_id``) has no live channel, so the
        latest response is saved to the object store instead, where the client
        reads it via ``GET /response/{id}`` (LOG updates are skipped).

        ``pickled`` publishes ``torch.save`` bytes rather than a JSON dump, so
        ``data`` can carry the result blob itself instead of a url to fetch it
        from. Only meaningful for a blocking request: the object store path
        keeps serving JSON, which is what ``GET /response/{id}`` returns.
        ``/subscribe`` tells the two apart by their first byte and forwards a
        pickled response as a binary frame.
        """
        from ..providers.objectstore import ObjectStoreProvider
        from ..providers.redis import RedisProvider

        response = self.response(status, description, data)

        if self.session_id:
            RedisProvider.sync_client.publish(
                self.session_id,
                response.pickle() if pickled else response.model_dump_json(),
            )
        elif status != Status.LOG:
            ObjectStoreProvider.put(
                _response_key(self.id),
                response.model_dump_json().encode(),
                content_type="application/json",
            )

        return response

    async def arespond(
        self,
        status: Status,
        description: str = "",
        data: Optional[Any] = None,
        pickled: bool = False,
    ) -> BackendResponseModel:
        """Async counterpart of :meth:`respond`.

        Used by the queue's async workers (dispatcher / processor / replica) so
        publishing a status update doesn't block the event loop on a sync Redis
        round-trip.
        """
        from ..providers.objectstore import ObjectStoreProvider
        from ..providers.redis import RedisProvider

        response = self.response(status, description, data)

        if self.session_id:
            await RedisProvider.async_client.publish(
                self.session_id,
                response.pickle() if pickled else response.model_dump_json(),
            )
        elif status != Status.LOG:
            # Non-blocking: persist the latest response for GET /response/{id}.
            await asyncio.to_thread(
                ObjectStoreProvider.put,
                _response_key(self.id),
                response.model_dump_json().encode(),
                content_type="application/json",
            )

        return response
