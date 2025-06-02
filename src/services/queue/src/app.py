from fastapi import FastAPI, Request, HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
import socketio
import boto3
import redis
import os
import time
import threading
import ray
from typing import Optional
from ray import serve
from fastapi_socketio import SocketManager
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
import uvicorn
import uuid

from nnsight.schema.response import ResponseModel
from .schema import BackendRequestModel
from .redis_manager import RedisQueueManager
from .dispatcher import Dispatcher
from .logging import load_logger

logger = load_logger(service_name="Queue", logger_name="Queue")

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

queue_manager = RedisQueueManager(
    os.environ.get("REDIS_HOST"),
    os.environ.get("REDIS_PORT"),
    os.environ.get("REDIS_DB"),
)

dispatcher = Dispatcher(queue_manager, os.environ.get("RAY_ADDRESS"))

def connect_to_ray():
    while True:
        try:
            if not ray.is_initialized():
                ray.shutdown()
                serve.context._set_global_client(None)
                ray.init(logging_level="error")
                logger.info("Connected to Ray cluster.")
        except Exception as e:
            logger.error(f"Failed to connect to Ray cluster: {e}")
            
        time.sleep(os.environ.get("RAY_RETRY_INTERVAL_S", 5))
        
        
# Start the background thread
ray_watchdog = threading.Thread(target=connect_to_ray, daemon=True)
ray_watchdog.start()

api_key_header = APIKeyHeader(name="ndif-api-key", auto_error=False)

Instrumentator().instrument(app).expose(app)

@app.on_event("startup")
async def startup_event():
    await dispatcher.start()

@app.on_event("shutdown")
async def shutdown_event():
    await dispatcher.stop()

def stream_connect(job_id: str):
    if sio.client is None:
        sio.connect(
            f"{os.environ.get('API_URL')}?job_id={job_id}",
            socketio_path="/ws/socket.io",
            transports=["websocket"],
            wait_timeout=10,
        )
        time.sleep(0.1)

@app.post("/queue")
async def queue(request: Request, api_key: str = Security(api_key_header)):
    """Endpoint to add a request to the queue."""
    
    try:
        # First, get the request body as bytes to avoid coroutine issues
        request_body = await request.body()
        
        # Create a modified request object with the resolved body
        backend_request = BackendRequestModel.from_request(
            request, api_key
        )

        logger.debug(f"Received request: {backend_request.id}")
        
        # Replace the coroutine graph with the actual bytes
        backend_request.graph = request_body
        queue_manager.enqueue(backend_request)
        logger.debug(f"Enqueued request: {backend_request.id}")

        stream_connect(backend_request.id)
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.APPROVED,
            description="Your job was approved and is waiting to be run.",
            logger=logger,
        ).respond(sio, object_store)
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=6001, workers=os.environ.get("WORKERS", 1))