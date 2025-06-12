import asyncio
import os
from typing import Optional

import boto3
import socketio
from fastapi import FastAPI, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from ray import serve

from nnsight.schema.response import ResponseModel

from .dispatcher import Dispatcher
from .logging import load_logger
from .queue_manager import QueueManager
from .schema import BackendRequestModel

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

queue_manager = QueueManager()
dispatcher = Dispatcher(queue_manager, os.environ.get("RAY_ADDRESS"))



Instrumentator().instrument(app).expose(app)

@app.on_event("startup")
async def startup_event():
    await dispatcher.start()
    # Start the socket connection task
    global connection_task
    connection_task = asyncio.create_task(manage_socket_connection())

@app.on_event("shutdown")
async def shutdown_event():
    await dispatcher.stop()
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

@app.post("/queue")
async def queue(request: Request):
    """Endpoint to add a request to the queue."""
    
    try:
        # Create a modified request object with the resolved body
        backend_request = BackendRequestModel.from_request(
            request  # TODO 
        )

        # Replace the coroutine graph with the actual bytes
        backend_request.request = await backend_request.request
        queue_manager.enqueue(backend_request)
        logger.debug(f"Enqueued request: {backend_request.id}")

        logger.debug(f"Creating response for request: {backend_request.id}")
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.APPROVED,
            description="Your job was approved and is waiting to be run.",
            logger=logger,
        )
        logger.debug(f"Responding to request: {backend_request.id}")
        logger.debug(f"SIO client is non-null: {sio.client is not None}")
        response.respond(sio, object_store)
        logger.debug(f"Responded to request: {backend_request.id}")
        return response
        
    except Exception as e:
        logger.error(f"Error processing queue request: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    
    
@app.head("/queue/{model_key}/{request_id}")
async def queue(model_key: Optional[str] = None):
    """Endpoint to verify the request is in the queue."""

    pass

@app.delete("/queue/{model_key}/{request_id}")
async def queue(model_key: Optional[str] = None):
    """Endpoint to delete a request from the queue."""

    pass


@app.get("/ping", status_code=200)
async def ping():
    """Endpoint to check if the server is online."""
    return "pong"


@app.get("/status", status_code=200)
async def status():
    """Endpoint to check if the server is online."""
    return await serve.get_app_handle("Controller").status.remote()