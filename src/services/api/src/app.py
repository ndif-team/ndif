import asyncio
import pickle
import traceback
import uuid
from typing import Any, Dict, Optional
from urllib.parse import parse_qs

import socketio
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi_socketio import SocketManager


from nnsight.schema.response import ResponseModel

from .logging import set_logger
from .types import REQUEST_ID, SESSION_ID

logger = set_logger("API")

from .config import AppConfig
from .dependencies import validate_request, require_ray_connection
from .metrics import NetworkStatusMetric
from .providers.objectstore import ObjectStoreProvider
from .providers.redis import RedisProvider
from .schema import BackendRequestModel, BackendResponseModel

# Init FastAPI app
app = FastAPI()

try:
    from prometheus_fastapi_instrumentator import Instrumentator

    # Prometheus instrumentation (for metrics)
    Instrumentator().instrument(app).expose(app)
except ImportError as e:
    pass


# Add middleware for CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Init async manager for communication between socketio servers
socketio_manager = socketio.AsyncRedisManager(url=AppConfig.broker_url)
# Init socketio manager app
sm = SocketManager(
    app=app,
    mount_location="/ws",
    client_manager=socketio_manager,
    max_http_buffer_size=AppConfig.socketio_max_http_buffer_size,
    ping_timeout=AppConfig.socketio_ping_timeout,
    always_connect=True,
)

# Init object_store connection
ObjectStoreProvider.connect()


@app.post("/request", dependencies=[Depends(require_ray_connection)])
async def request(
    background_tasks: BackgroundTasks,
    backend_request: BackendRequestModel = Depends(validate_request),
) -> BackendResponseModel:
    """Endpoint to submit request. See src/common/schema/request.py to see the headers and data that are validated and populated.

    Args:
        background_tasks: FastAPI background tasks manager.
        backend_request: Validated BackendRequestModel with all headers and data populated.

    Returns:
        BackendResponseModel: Response to the user request containing job status and metadata.
    """

    # process the request
    try:
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.RECEIVED,
            description="Your job has been received and is waiting to be queued.",
            logger=logger,
        )

        if not response.blocking:
            response.save()

        # Run network status metric update in background
        background_tasks.add_task(NetworkStatusMetric.update, backend_request)

        backend_request.request = await backend_request.request

        await RedisProvider.async_client.lpush("queue", pickle.dumps(backend_request))

    except Exception as exception:
        description = f"{traceback.format_exc()}\n{str(exception)}"

        # Create exception response object.
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=description,
            logger=logger,
        )

    # Return response.
    return response


@sm.on("connect")
async def connect(session_id: SESSION_ID, environ: Dict[str, Any]) -> None:
    """Handle new SocketIO client connections.

    Parses the query string from the connection request and adds the client
    to a room based on their job_id if provided. This allows targeted message
    delivery to specific job subscribers.

    Args:
        session_id: Unique identifier for this SocketIO session.
        environ: WSGI environ dict containing connection metadata including
            QUERY_STRING with optional job_id parameter.
    """
    query_string = environ.get("QUERY_STRING", "")
    params = parse_qs(query_string)

    # parse_qs returns lists, get first value if present
    job_id = params.get("job_id", [None])[0]

    if job_id:
        await sm.enter_room(session_id, job_id)


@sm.on("blocking_response")
async def blocking_response(
    session_id: SESSION_ID, client_session_id: SESSION_ID, data: Any
) -> None:
    """Forward a blocking response to a specific client session.

    Used internally to relay response data from the backend to the waiting
    client. This is the final step in the blocking request flow.

    Args:
        session_id: The sender's SocketIO session ID.
        client_session_id: The target client's SocketIO session ID to receive
            the response.
        data: The response data to forward (typically pickled BackendResponseModel).
    """
    await sm.emit("blocking_response", data=data, to=client_session_id)


@sm.on("stream")
async def stream(
    session_id: SESSION_ID, client_session_id: SESSION_ID, data: bytes, job_id: str
) -> None:
    """Handle streaming response initiation.

    Registers the sender in the job's room for future streaming updates,
    then forwards the initial response data to the client.

    Args:
        session_id: The sender's SocketIO session ID.
        client_session_id: The target client's SocketIO session ID.
        data: Initial response data to forward.
        job_id: The job identifier used as the room name for streaming.
    """
    await sm.enter_room(session_id, job_id)

    await blocking_response(session_id, client_session_id, data)


@sm.on("stream_upload")
async def stream_upload(session_id: SESSION_ID, data: bytes, job_id: str) -> None:
    """Broadcast streaming data to all subscribers of a job.

    Emits data to all clients in the job's room, enabling real-time
    streaming of intermediate results or progress updates.

    Args:
        session_id: The sender's SocketIO session ID.
        data: The streaming data to broadcast.
        job_id: The job identifier (room name) to broadcast to.
    """
    await sm.emit("stream_upload", data=data, room=job_id)


@app.get("/response/{id}")
async def response(id: REQUEST_ID) -> BackendResponseModel:
    """Endpoint to get latest response for id.

    Args:
        id: ID of request/response.

    Returns:
        BackendResponseModel: Response.
    """

    # Load response from client given id.
    return BackendResponseModel.load(id)


@app.get("/ping", status_code=200)
async def ping():
    """Endpoint to check if the server is online."""
    return "pong"


@app.get("/connected", status_code=200, dependencies=[Depends(require_ray_connection)])
async def connected():
    """Endpoint to check if Ray cluster is connected."""
    return {"status": "connected"}


@app.get("/status", status_code=200, dependencies=[Depends(require_ray_connection)])
async def status() -> Dict[str, Any]:
    """Get the current cluster status.

    Returns cached status if available, otherwise triggers a status request
    to the Controller and waits for the response via Redis pub/sub.

    Returns:
        Dictionary containing cluster status information including deployed
        models, resource usage, and availability.

    Raises:
        HTTPException: If status request times out (504 Gateway Timeout).

    See Also:
        AppConfig.status_request_timeout_s: Configures the timeout duration.
    """
    # Check for cached status first
    cached_status = await RedisProvider.async_client.get("status")
    if cached_status is not None:
        return pickle.loads(cached_status)

    # No cached status, need to request and wait for it
    pubsub = RedisProvider.async_client.pubsub()

    try:
        await pubsub.subscribe("status:event")

        # Trigger status request if not already requested
        if await RedisProvider.async_client.set("status:requested", "1", nx=True):
            await RedisProvider.async_client.xadd(
                "status:trigger", {"reason": "requested"}
            )

        # Check again in case status was cached while we were setting up
        cached_status = await RedisProvider.async_client.get("status")
        if cached_status is not None:
            return pickle.loads(cached_status)

        # Wait for status event with timeout
        async def wait_for_status() -> Optional[bytes]:
            """Wait for status message from pub/sub."""
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                return message["data"]
            return None

        try:
            status_data = await asyncio.wait_for(
                wait_for_status(), timeout=AppConfig.status_request_timeout_s
            )
            if status_data is not None:
                return pickle.loads(status_data)
            else:
                raise HTTPException(
                    status_code=503,
                    detail="Status unavailable: pub/sub stream ended unexpectedly",
                )
        except asyncio.TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"Status request timed out after {AppConfig.status_request_timeout_s} seconds",
            )

    finally:
        # Always cleanup pubsub resources
        await pubsub.unsubscribe("status:event")
        await pubsub.aclose()


@app.get("/env", status_code=200, dependencies=[Depends(require_ray_connection)])
async def env() -> Dict[str, Any]:
    """Get the Python environment information from the Ray cluster.

    Returns cached env info if available, otherwise sends a request to the
    Dispatcher which queries the Controller for the environment details.

    Returns:
        Dictionary containing Python version and installed pip packages.

    Raises:
        HTTPException: If the request times out (504 Gateway Timeout).
    """
    # Check for cached env first
    cached_env = await RedisProvider.async_client.get("env")
    if cached_env is not None:
        return pickle.loads(cached_env)

    # No cached env, send event to dispatcher
    response_key = f"env:response:{uuid.uuid4()}"

    try:
        # Send ENV event to dispatcher
        await RedisProvider.async_client.xadd(
            "dispatcher:events",
            {
                "event_type": "env",
                "response_key": response_key,
            },
        )

        # Wait for response with timeout
        result = await RedisProvider.async_client.brpop(
            response_key, timeout=AppConfig.status_request_timeout_s
        )

        if result is None:
            raise HTTPException(
                status_code=504,
                detail=f"Env request timed out after {AppConfig.status_request_timeout_s} seconds",
            )

        env_data = pickle.loads(result[1])

        if "error" in env_data:
            raise HTTPException(
                status_code=503,
                detail=f"Error getting env info: {env_data['error']}",
            )

        return env_data

    finally:
        # Clean up the response key
        await RedisProvider.async_client.delete(response_key)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=1)
