import asyncio
import logging
import pickle
import time
import uuid

from typing import Any, Dict, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ndif.common.metrics import RequestSizeMetric, RequestStatusTimeMetric
from ndif.common.providers.objectstore import ObjectStoreProvider
from ndif.common.providers.redis import RedisProvider
from ndif.common.schema import BackendRequestModel, BackendResponseModel, Status
from ndif.common.telemetry import event
from ndif.common.redis import (
    ENV_KEY,
    ENV_READY_CHANNEL,
    ENV_REQUESTED_KEY,
    ENV_TIMEOUT_S,
    ENV_TRIGGER_STREAM,
    STATUS_KEY,
    STATUS_READY_CHANNEL,
    STATUS_REQUESTED_KEY,
    STATUS_TIMEOUT_S,
    STATUS_TRIGGER_STREAM,
)

from .auth import validate_request, verify_api_key
from .versioning import validate_client_versions
from .queue.config import CONFIG as QUEUE_CONFIG

# Telemetry sinks (Loki/InfluxDB) are connected per worker in gunicorn's
# post_fork hook (see gunicorn_conf), so this module doesn't touch them.
logger = logging.getLogger("ndif.api")

app = FastAPI(title="NDIF API")

# Auth is header-based (ndif-api-key), not cookie-based, so credentials aren't
# needed — and "*" origins with allow_credentials=True is a spec conflict
# browsers reject. Keep credentials off so a wildcard origin is valid.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def log_http_exception(
    http_request: Request, exc: HTTPException
) -> JSONResponse:
    """Emit a structured event for any HTTP error, then return the standard
    response. Ingress failures (a rejected key, an unavailable backend, ...) raise
    HTTPException before reaching an endpoint body, so this is where they become
    observable. Server errors (5xx) log at ERROR; client errors (4xx) at INFO.
    """
    event(
        logger,
        f"api request rejected: {exc.status_code}",
        level=logging.ERROR if exc.status_code >= 500 else logging.INFO,
        path=http_request.url.path,
        status_code=exc.status_code,
        detail=str(exc.detail),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def log_unhandled_exception(
    http_request: Request, exc: Exception
) -> JSONResponse:
    """Log an unhandled failure (enqueue, a provider, ...) as a structured ERROR
    event with its traceback, then return a generic 500 — so it's observable rather
    than only uvicorn-logged, and internals never leak to the client."""
    event(
        logger,
        "api unhandled error",
        level=logging.ERROR,
        exc_info=True,
        path=http_request.url.path,
        error_type=type(exc).__name__,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error."})


async def require_ray_connection() -> None:
    """Reject requests when the compute backend (Ray) isn't reachable.

    The dispatcher maintains the ``ray:connected`` Redis flag (set once
    connected, deleted while reconnecting). Used as a route dependency to
    short-circuit with 503 before doing any work, rather than enqueuing a
    request the backend can't serve.
    """
    if not await RedisProvider.async_client.get("ray:connected"):
        raise HTTPException(
            status_code=503,
            detail="Service temporarily unavailable: compute backend is "
            "reconnecting. Please try again in a few minutes.",
        )


@app.post("/request", dependencies=[Depends(require_ray_connection)])
async def create_request(
    http_request: Request,
    request: BackendRequestModel = Depends(validate_request),
    blob: UploadFile = File(..., description="Serialized request blob"),
    user_agent: Optional[str] = Header(default=None),
    ndif_timestamp: Optional[str] = Header(default=None),
    nnsight_version: Optional[str] = Header(default=None),
    python_version: Optional[str] = Header(default=None),
) -> BackendResponseModel:
    """Accept a multipart request: structured JSON `data` plus a binary `blob`.

    Returns the initial RECEIVED response over HTTP; all subsequent status
    updates are published to the client's `/subscribe` websocket.
    """
    # Reject incompatible clients early. A no-op unless NDIF_MIN_NNSIGHT_VERSION
    # / NDIF_MIN_PYTHON_VERSION are configured; otherwise 400s an old client
    # (from the nnsight-version / python-version headers) before any work.
    validate_client_versions(nnsight_version, python_version)

    # The request has been parsed, key-verified, and stamped with the caller's
    # email + trusted flag by the validate_request dependency (see auth.py).

    # The blob (serialized interventions) rides on the request through the
    # queue; the dispatcher routes it and the model actor runs it via Ray.
    # TODO: the payload size is measured downstream but never enforced — add a
    # configurable request-size cap (an NDIF_* limit) and reject oversized blobs
    # here before they enter the queue.
    request.payload = await blob.read()

    # First status-time hop: client->server transit. The client stamps its send
    # time in the ndif-timestamp header; if present (and not obvious clock skew),
    # record received - sent as the "SENT" bucket. Then seed the clock at
    # RECEIVED so the next hop (QUEUED) bills the RECEIVED->queue gap.
    # last_status_time doubles as the ingress timestamp and travels with the
    # pickled request through the dispatcher.
    received_at = time.time()
    sent_at = None
    if ndif_timestamp:
        try:
            sent_at = float(ndif_timestamp)
        except ValueError:
            sent_at = None
    if sent_at is not None and received_at >= sent_at:
        RequestStatusTimeMetric.update(
            model_key=request.model_key,
            request_id=request.id,
            api_key=request.api_key,
            email=request.email,
            status="SENT",
            duration_ms=round((received_at - sent_at) * 1000, 2),
        )
    # Advance to RECEIVED — seeds the clock so the next hop (QUEUED) bills the
    # RECEIVED->queue gap, and last_status_time travels with the pickled request —
    # and build the response we return once the request is enqueued.
    response = request.response(
        Status.RECEIVED, "Your job has been received and is waiting to be queued."
    )

    # Enqueue for the dispatcher (lpush + the dispatcher's brpop = FIFO). The
    # request is pickled, so this uses the binary Redis client.
    await RedisProvider.async_bytes_client.lpush(
        QUEUE_CONFIG.queue_key, pickle.dumps(request)
    )

    # Caller identity: only needed here, for the ingress metric — not stored on
    # the request (nothing downstream reads it).
    ip_address = http_request.client.host if http_request.client else None

    RequestSizeMetric.update(
        model_key=request.model_key,
        request_id=request.id,
        api_key=request.api_key,
        email=request.email,
        session_id=request.session_id,
        ip_address=ip_address,
        user_agent=user_agent,
        payload_bytes=len(request.payload) if request.payload else 0,
    )

    return response


async def _coalesced_fetch(
    *,
    cache_key: str,
    requested_key: str,
    trigger_stream: str,
    ready_channel: str,
    timeout_s: int,
    subject: str,
) -> Response:
    """Serve a Redis-cached JSON blob refreshed by a dispatcher worker.

    Both /status and /env work this way: the API can't reach Ray, so on a cache
    miss concurrent callers coalesce through a SET NX lock (``requested_key``)
    and only the winner appends to ``trigger_stream``; the dispatcher's worker
    fetches from the controller, writes ``cache_key``, and publishes to
    ``ready_channel`` to wake the waiters. Bounded by ``timeout_s``.

    503 if the refresh failed, 504 if it didn't arrive in time.
    """
    redis = RedisProvider.async_client

    cached = await redis.get(cache_key)
    if cached is not None:
        return Response(content=cached, media_type="application/json")

    pubsub = redis.pubsub()
    await pubsub.subscribe(ready_channel)
    try:
        # Double-check after subscribing so a refresh that landed between the
        # first GET and the subscribe isn't missed.
        cached = await redis.get(cache_key)
        if cached is not None:
            return Response(content=cached, media_type="application/json")

        # Coalesce: only the request that wins the lock triggers a refresh.
        if await redis.set(requested_key, "1", nx=True, ex=timeout_s):
            await redis.xadd(
                trigger_stream, {"t": "1"}, maxlen=16, approximate=True
            )

        try:
            async with asyncio.timeout(timeout_s):
                async for message in pubsub.listen():
                    if message["type"] != "message":
                        continue
                    if message["data"] == "error":
                        raise HTTPException(
                            status_code=503,
                            detail=f"Could not retrieve {subject}.",
                        )
                    cached = await redis.get(cache_key)
                    if cached is not None:
                        return Response(
                            content=cached, media_type="application/json"
                        )
        except TimeoutError:
            raise HTTPException(
                status_code=504, detail=f"Timed out waiting for {subject}."
            )
    finally:
        await pubsub.unsubscribe(ready_channel)
        await pubsub.aclose()

    raise HTTPException(status_code=503, detail=f"Could not retrieve {subject}.")


@app.get("/status", dependencies=[Depends(require_ray_connection)])
async def get_status() -> Response:
    """Return the cluster status: a JSON blob cached in Redis with a TTL.

    On a cache miss, concurrent callers coalesce through a SET NX lock so only
    one triggers the dispatcher's status worker (a heavy controller fetch); the
    rest wait to be woken with the fresh value. Bounded by STATUS_TIMEOUT_S.

    503 if the refresh failed, 504 if it didn't arrive in time.
    """
    return await _coalesced_fetch(
        cache_key=STATUS_KEY,
        requested_key=STATUS_REQUESTED_KEY,
        trigger_stream=STATUS_TRIGGER_STREAM,
        ready_channel=STATUS_READY_CHANNEL,
        timeout_s=STATUS_TIMEOUT_S,
        subject="cluster status",
    )


@app.get("/env", dependencies=[Depends(require_ray_connection)])
async def get_env() -> Response:
    """Return the cluster's python version + installed packages (for the CLI to
    match environments). Same coalesced-cache mechanism as /status, refreshed by
    the dispatcher's env worker.
    """
    return await _coalesced_fetch(
        cache_key=ENV_KEY,
        requested_key=ENV_REQUESTED_KEY,
        trigger_stream=ENV_TRIGGER_STREAM,
        ready_channel=ENV_READY_CHANNEL,
        timeout_s=ENV_TIMEOUT_S,
        subject="cluster env",
    )


@app.get("/response/{id}")
async def get_response(id: str) -> Response:
    """Return the latest status response for a non-blocking request.

    A non-blocking request has no `/subscribe` websocket, so its responses are
    saved to the object store as they progress (see BackendRequestModel.respond);
    the client polls this endpoint by request id. 404 until the first response
    lands (or if the id is unknown).
    """
    data = await asyncio.to_thread(
        ObjectStoreProvider.get, f"responses/{id}.json"
    )
    if data is None:
        raise HTTPException(status_code=404, detail=f"No response for request {id}.")
    return Response(content=data, media_type="application/json")


@app.get("/ping")
async def ping() -> str:
    """Liveness probe: returns ``"pong"`` if the API process is up."""
    return "pong"


@app.api_route(
    "/connected",
    methods=["GET", "HEAD"],
    dependencies=[Depends(require_ray_connection)],
)
async def connected() -> Dict[str, str]:
    """Report whether the compute backend (Ray) is reachable.

    The ``require_ray_connection`` dependency 503s when the cluster is down or
    reconnecting; reaching the body means it's connected.
    """
    return {"status": "connected"}


@app.get("/whoami")
async def whoami(
    api_key: Optional[str] = Header(default=None, alias="ndif-api-key"),
) -> Dict[str, Any]:
    """Resolve the email + tags associated with the caller's API key.

    Reads the key from the ``ndif-api-key`` header. Unlike the request path, an
    unknown/missing/malformed key is not an error here — it just resolves to
    ``{"email": None, "tags": []}`` (matching the pre-existing public contract,
    where ``tags`` are the key's user_tags). A DB outage still surfaces as 503.
    """
    try:
        identity = await verify_api_key(api_key)
    except HTTPException as exc:
        # 401 (missing/malformed) and 403 (unknown key) are non-fatal for a
        # lookup; only propagate a real backend failure (503).
        if exc.status_code in (401, 403):
            return {"email": None, "tags": []}
        raise

    # identity is None when auth is disabled (no Postgres configured).
    if identity is None:
        return {"email": None, "tags": []}

    return {"email": identity.email, "tags": identity.user_tags}


@app.websocket("/subscribe")
async def subscribe(websocket: WebSocket):
    """Open a session and stream its status updates to the client.

    The server generates a `session_id` and sends it as the first message
    (`{"session_id": "..."}`). The client echoes that id back as the
    `session_id` of a subsequent `POST /request`. Thereafter, every Response
    published to the Redis channel named by that id (already a JSON string) is
    forwarded down this socket.
    """
    await websocket.accept()

    session_id = uuid.uuid4().hex

    # Subscribe BEFORE handing the client its session id. The client only POSTs
    # /request after receiving the id, so subscribing first guarantees the
    # channel is live before any status can be published — no lost-message race.
    pubsub = RedisProvider.async_client.pubsub()
    await pubsub.subscribe(session_id)

    await websocket.send_json({"session_id": session_id})

    async def forward() -> None:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])

    async def watch_disconnect() -> None:
        # The client never sends on this socket, so receive() returns only when
        # it disconnects (raising WebSocketDisconnect). Without this, an idle
        # client that drops would leave forward() blocked in listen() forever.
        try:
            while True:
                await websocket.receive()
        except WebSocketDisconnect:
            return

    forward_task = asyncio.create_task(forward())
    disconnect_task = asyncio.create_task(watch_disconnect())

    try:
        # Whichever finishes first (forwarder erroring, or client disconnect)
        # tears the other down.
        await asyncio.wait(
            {forward_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in (forward_task, disconnect_task):
            task.cancel()
        # Await the tasks we just cancelled so their teardown completes. The
        # CancelledError they raise is expected (it's the cancel we requested);
        # return_exceptions captures it here instead of letting it bubble out of
        # the handler as a logged "Exception in ASGI application". (CancelledError
        # is a BaseException, so a plain `except Exception` / suppress(Exception)
        # would miss it.) An outer cancel of this coroutine still propagates.
        await asyncio.gather(forward_task, disconnect_task, return_exceptions=True)
        await pubsub.unsubscribe(session_id)
        await pubsub.aclose()
