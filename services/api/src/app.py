import asyncio
import os
import traceback
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict

import ray
import socketio
import uvicorn
from fastapi import FastAPI, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.decorator import cache
from fastapi_socketio import SocketManager
from minio import Minio
from influxdb_client import Point
from prometheus_fastapi_instrumentator import Instrumentator
from ray import serve

from nnsight.schema.response import ResponseModel

from .api_key import api_key_auth
from .logging import load_logger
from .metrics import RequestStatusMetric, TransportLatencyMetric
from .schema import BackendRequestModel, BackendResponseModel, BackendResultModel

logger = load_logger(service_name="app", logger_name="gunicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize FastAPI cache on startup.
    
    Args:
        app (FastAPI): The FastAPI application instance.
    """
    FastAPICache.init(InMemoryBackend())
    yield


# Init FastAPI app
app = FastAPI(lifespan=lifespan)

# Add middleware for CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Init async manager for communication between socketio servers
socketio_manager = socketio.AsyncRedisManager(
    url=os.environ.get("BROKER_URL"),
)

# Init socketio manager app
sm = SocketManager(
    app=app,
    mount_location="/ws",
    client_manager=socketio_manager,
    max_http_buffer_size=0,  # No limit on buffer size
    ping_timeout=60,  # 60 second ping timeout
    ping_interval=25,  # Send ping every 25 seconds
    cors_allowed_origins="*",  # Allow all origins
    async_mode="asgi",  # Use eventlet for better performance
    logger=True,  # Enable logging
    engineio_logger=True,  # Enable Engine.IO logging
    always_connect=True,  # Always connect to the server
    reconnection=True,  # Enable reconnection
    reconnection_attempts=5,  # Maximum number of reconnection attempts
    reconnection_delay=1,  # Initial delay between reconnection attempts
    reconnection_delay_max=5,  # Maximum delay between reconnection attempts
    reconnection_timeout=20,  # Timeout for reconnection attempts
    allow_upgrades=True,  # Allow transport upgrades
    http_compression=True,  # Enable HTTP compression
    websocket_compression=True,  # Enable WebSocket compression
    websocket_per_message_deflate=True,  # Enable per-message deflate
    websocket_max_message_size=0,  # No limit on message size
    max_connections=0,  # No limit on concurrent connections
    max_queue_size=0,  # No limit on queue size
)

# Init object_store connection
object_store = Minio(
    os.environ.get("OBJECT_STORE_URL"),
    access_key=os.environ.get("OBJECT_STORE_ACCESS_KEY", "minioadmin"),
    secret_key=os.environ.get("OBJECT_STORE_SECRET_KEY", "minioadmin"),
    secure=False,
)

# Init Ray connection
ray.init(logging_level="error")

# Prometheus instrumentation (for metrics)
Instrumentator().instrument(app).expose(app)

api_key_header = APIKeyHeader(name="ndif-api-key", auto_error=False)

# Create a dummy request to ensure app handle is created
# try:
#     serve.get_app_handle("Request").remote({})
# except:
#     pass


@app.post("/request")
async def request(
    raw_request: Request, api_key: str = Security(api_key_header)
) -> BackendResponseModel:
    """Submit a new request for processing.

    This endpoint handles the submission of new requests for model inference.
    It validates the request, authenticates the API key, and queues the request
    for processing by the Ray workers.

    Args:
        raw_request (Request): The raw HTTP request containing the intervention graph
        api_key (str): The API key for authentication

    Returns:
        BackendResponseModel: Response containing the request status and job ID

    Raises:
        Exception: If request validation or processing fails
    """
    # extract the request data
    try:
        request: BackendRequestModel = await BackendRequestModel.from_request(
            raw_request, api_key
        )
    except Exception as e:
        description = f"{traceback.format_exc()}\n{str(e)}"
        logger.error(f"{ResponseModel.JobStatus.ERROR.name}: {description}")

        labels = {
            "request_id": "",
            "api_key": api_key,
            "model_key": "",
            "user_id": "",
            "msg": description,
        }

        point: Point = Point(RequestStatusMetric.name)
        for tag, value in labels.items():
            point.tag(tag, value)
        point.field(
            RequestStatusMetric.name, RequestStatusMetric.NumericJobStatus.ERROR.value
        )
        super(RequestStatusMetric, RequestStatusMetric).update(point)
        raise e

    # process the request
    try:
        TransportLatencyMetric.update(request)
        response = request.create_response(
            status=ResponseModel.JobStatus.RECEIVED,
            description="Your job has been received and is waiting approval.",
            logger=logger,
        )

        # authenticate api key
        api_key_auth(raw_request, request)

        # Send to request workers waiting to process requests on the "request" queue
        serve.get_app_handle("Request").remote(request)

        # Back up request object by default (to be deleted on successful completion)
        # request = request.model_copy()
        # request.object = object
        # request.save(object_store)
    except Exception as exception:
        description = f"{traceback.format_exc()}\n{str(exception)}"
        response = request.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=description,
            logger=logger,
        )

    if not response.blocking:
        response.save(object_store)

    return response


@sm.on("connect")
async def connect(session_id: str, environ: Dict):
    """Handle new WebSocket connections.
    
    When a client connects, they can specify a job_id in the query parameters.
    If provided, the client is added to a room for that specific job. This allows users to stream data from their local machine to the model deployment processing their request.

    Args:
        session_id (str): Unique identifier for the WebSocket session
        environ (Dict): Environment variables containing query parameters
    """
    try:
        params = environ.get("QUERY_STRING", "")
        if params:
            params = dict(x.split("=") for x in params.split("&"))

            if "job_id" in params:
                await sm.enter_room(session_id, params["job_id"])
                logger.info(f"Client {session_id} joined room {params['job_id']}")
    except Exception as e:
        logger.error(f"Error in connect handler: {str(e)}")
        raise


@sm.on("disconnect")
async def disconnect(session_id: str):
    """Handle client disconnections.
    
    Args:
        session_id (str): ID of the disconnected client
    """
    try:
        # Clean up any rooms the client was in
        rooms = await sm.get_session(session_id)
        if rooms:
            for room in rooms:
                await sm.leave_room(session_id, room)
        logger.info(f"Client {session_id} disconnected")
    except Exception as e:
        logger.error(f"Error in disconnect handler: {str(e)}")


@sm.on("error")
async def error(session_id: str, error: Exception):
    """Handle WebSocket errors.
    
    Args:
        session_id (str): ID of the client that encountered the error
        error (Exception): The error that occurred
    """
    logger.error(f"WebSocket error for client {session_id}: {str(error)}")


@sm.on("blocking_response")
async def blocking_response(session_id: str, client_session_id: str, data: Any):
    """Handle blocking responses from workers.
    
    Forwards blocking responses to the specific client that requested them.

    Args:
        session_id (str): ID of the current WebSocket session
        client_session_id (str): ID of the client to receive the response
        data (Any): The response data to forward
    """
    await sm.emit("blocking_response", data=data, to=client_session_id)


@sm.on("stream_upload")
async def stream_upload(session_id: str, data: bytes, job_id: str):
    """Handle streaming uploads from clients.
    
    Broadcasts uploaded data to all clients in the specified job's room.

    Args:
        session_id (str): ID of the current WebSocket session
        data (bytes): The uploaded data
        job_id (str): ID of the job room to broadcast to
    """
    await sm.emit("stream_upload", data=data, room=job_id)


@app.get("/response/{id}")
async def response(id: str) -> BackendResponseModel:
    """Get the latest response for a specific request ID.

    Args:
        id (str): The unique identifier of the job/response

    Returns:
        BackendResponseModel: The response containing status.
    """
    return BackendResponseModel.load(object_store, id)


@app.get("/result/{id}")
async def result(id: str) -> BackendResultModel:
    """Stream the result data for a specific request ID.

    This endpoint streams the result data in chunks and cleans up the stored
    data after streaming is complete. Uses 8KB chunks for optimal performance
    while maintaining smooth progress updates.

    Args:
        id (str): The unique identifier of the request/response

    Returns:
        StreamingResponse: A streaming response containing the result data
    """
    object = BackendResultModel.load(object_store, id, stream=True)
    
    headers = {
        "Content-Length": object.headers["Content-Length"],
        "Content-Type": "application/octet-stream",
        "Cache-Control": "no-cache",
    }

    def stream():
        chunk_size = 8192  # 8KB chunks for optimal performance
        
        try:
            while True:
                data = object.read(chunk_size)
                if not data:
                    break
                yield data
        finally:
            object.close()
            object.release_conn()

            # Clean up stored data after streaming
            BackendResultModel.delete(object_store, id)
            BackendResponseModel.delete(object_store, id)
            BackendRequestModel.delete(object_store, id)

    return StreamingResponse(
        content=stream(),
        media_type="application/octet-stream",
        headers=headers,
        background=None,  # Ensure cleanup happens after streaming
    )


@app.get("/ping", status_code=200)
async def ping():
    """Health check endpoint to verify the server is running.

    Returns:
        str: "pong" if the server is operational
    """
    return "pong"


@app.get("/status", status_code=200)
@cache(expire=600)
async def status():
    """Get the current status of all model deployments.

    This endpoint returns information about all deployed models, including their
    current status, configuration, and scheduling information if available.
    The response is cached for 10 minutes to reduce load on the system.

    Returns:
        Dict: A dictionary containing:
            - deployments: Information about all deployed models
            - calendar_id: ID of the Google Calendar used for scheduling (if applicable)
    """
    status = await serve.get_app_handle(
        "Controller"
    ).status.remote()
    return status


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=1)
