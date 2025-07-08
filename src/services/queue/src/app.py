import asyncio
import boto3
import os
import traceback
import socketio
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.security.api_key import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from nnsight.schema.response import ResponseModel
from .schema import BackendRequestModel
from .logging import set_logger
from .coordination.request_coordinator import RequestCoordinator

logger = set_logger("Queue")

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
api_url = os.environ.get('API_URL')
sio.connect(
    api_url,
    socketio_path="/ws/socket.io",
    transports=["websocket"],
    wait_timeout=100000,
)
connection_event = asyncio.Event()
connection_task = None

ray_url = os.environ.get("RAY_ADDRESS")
max_consecutive_failures = os.environ.get("MAX_CONSECUTIVE_FAILURES", 5)

coordinator = RequestCoordinator(ray_url=ray_url, sio=sio, object_store=object_store)
coordinator.start()

Instrumentator().instrument(app).expose(app)

dev_mode = os.environ.get("DEV_MODE", True) # TODO: default to false

@app.get("/queue")
async def get_queue_state(return_batch: bool = False):
    """Get complete queue state."""
    if dev_mode and return_batch:
        return coordinator.get_previous_states()
    else:
        return coordinator.get_state()

@app.post("/queue")
async def queue(request: Request):
    """Endpoint to add a request to the queue."""
    
    try:
        headers = request.headers
        # Create a modified request object with the resolved body
        backend_request = BackendRequestModel.from_request(
            request
        )
        try: 
            logger.debug(f"Creating response for request: {backend_request.id}")
            response = backend_request.create_response(
                status=ResponseModel.JobStatus.APPROVED,
                description="Your job was approved and is waiting to be run.",
                logger=logger,
            )
            response.respond(sio, object_store)
            logger.debug(f"Responded to request: {backend_request.id}")
        except Exception as e:
            logger.error(f"Failed to respond APPROVED message for request {backend_request.id}")
 
        # Replace the coroutine graph with the actual bytes
        backend_request.graph = await backend_request.graph
        
        coordinator.route_request(backend_request)
        
        logger.debug(f"Enqueued request: {backend_request.id}")

        return response
        
    except Exception as e:
        logger.error(f"Error processing queue request: {str(e)}")
        description = f"{traceback.format_exc()}\n{str(e)}"
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=description,
            logger=logger,
        )
        try:
            response.respond(sio, object_store)
        except:
            logger.error(f"Failed responding ERROR to user: {description}")
        return response

@app.delete("/queue/{request_id}")
async def remove_request(request_id: str):
    """Remove a request from the queue."""
    try:
        coordinator.remove_request(request_id)
    except Exception as e:
        logger.error(f"Error removing request {request_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Error removing request {request_id}: {e}")

@app.get("/ping", status_code=200)
async def ping():
    """Endpoint to check if the server is online."""
    return "pong"
