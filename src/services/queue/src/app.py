import asyncio
import boto3
import os
import traceback
import socketio
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from nnsight.schema.response import ResponseModel
from .schema import BackendRequestModel
from .logging import load_logger
from .coordination.request_coordinator import RequestCoordinator

logger = load_logger(service_name="QUEUE", logger_name="QUEUE")

app = FastAPI()

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Init object_store connection
object_store = boto3.client(
    os.environ.get("OBJECT_STORE_SERVICE", "s3"),
    endpoint_url=f"http://{os.environ.get('OBJECT_STORE_URL')}",
    aws_access_key_id=os.environ.get("OBJECT_STORE_ACCESS_KEY", "minioadmin"),
    aws_secret_access_key=os.environ.get("OBJECT_STORE_SECRET_KEY", "minioadmin"),
    region_name=os.environ.get("OBJECT_STORE_REGION", 'us-east-1'),
    verify=os.environ.get("OBJECT_STORE_VERIFY", False),
    # Set to path style for compatibility with non-AWS S3 implementations
    config=boto3.session.Config(signature_version='s3v4', s3={'addressing_style': 'path'})
)

sio = socketio.SimpleClient(reconnection_attempts=10)
connection_event = asyncio.Event()
connection_task = None

ray_url = os.environ.get("RAY_ADDRESS")
coordinator = RequestCoordinator(ray_url=ray_url)

api_key_header = APIKeyHeader(name="ndif-api-key", auto_error=False)

Instrumentator().instrument(app).expose(app)

dev_mode = os.environ.get("DEV_MODE", True) # TODO: default to false

@app.on_event("startup")
async def startup_event():
    await coordinator.start()
    # Start the socket connection task
    global connection_task
    connection_task = asyncio.create_task(manage_socket_connection())

@app.on_event("shutdown")
async def shutdown_event():
    await coordinator.stop()
    if connection_task:
        connection_task.cancel()
        try:
            await connection_task
        except asyncio.CancelledError:
            pass

async def manage_socket_connection():
    """Background task to manage socket connection."""
    while True:
        try:
            if not sio.connected:
                api_url = os.environ.get('API_URL')
                sio.connected = False
                sio.connect(
                    api_url,
                    socketio_path="/ws/socket.io",
                    transports=["websocket"],
                    wait_timeout=100000,
                )
                connection_event.set()
                logger.debug(f"Connected to SocketIO mount: {api_url}")
            await asyncio.sleep(1)  # Check connection status periodically
        except Exception as e:
            logger.error(f"Failed to connect to API: {e}")
            connection_event.clear()
            await asyncio.sleep(5)  # Wait before retrying

async def ensure_socket_connection(request: Request, call_next):
    """Middleware to ensure socket connection for /queue endpoint."""
    if request.url.path == "/queue" and request.method == "POST":
        if not sio.connected:
            # Wait for connection with timeout
            try:
                await asyncio.wait_for(connection_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Socket connection timeout")
    
    response = await call_next(request)
    return response

app.middleware("http")(ensure_socket_connection)

@app.get("/queue")
async def get_queue_state(return_batch: bool = False):
    """Get complete queue state."""
    if dev_mode and return_batch:
        return coordinator.get_previous_states()
    else:
        return coordinator.state()

@app.post("/queue")
async def queue(request: Request):
    """Endpoint to add a request to the queue."""
    
    try:
        # Create a modified request object with the resolved body
        backend_request = BackendRequestModel.from_request(
            request, request.headers.get("api_key")
        )
        backend_request.id = request.headers.get("request_id")

        # Replace the coroutine graph with the actual bytes
        backend_request.graph = await backend_request.graph
        await coordinator.route_request(backend_request)
        logger.debug(f"Enqueued request: {backend_request.id}")

        logger.debug(f"Creating response for request: {backend_request.id}")
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.APPROVED,
            description="Your job was approved and is waiting to be run.",
            logger=logger,
        )
        response.respond(sio, object_store)
        logger.debug(f"Responded to request: {backend_request.id}")
        return response
        
    except Exception as e:
        logger.error(f"Error processing queue request: {str(e)}")
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=f"{traceback.format_exc()}\n{str(e)}",
            logger=logger,
        )
        response.respond(sio, object_store)
        return response

@app.delete("/queue/{request_id}")
async def remove_request(request_id: str):
    """Remove a request from the queue."""
    # TODO: Support for removing requests from the queue
    pass

@app.get("/ping", status_code=200)
async def ping():
    """Endpoint to check if the server is online."""
    return "pong"