from fastapi import FastAPI, Request, HTTPException
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

logger = load_logger(service_name="API", logger_name="API")

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

sio = socketio.SimpleClient(
    url=os.environ.get("BROKER_URL"),
    socketio_path="/ws/socket.io",
    transports=["websocket"],
    wait_timeout=100000,
)

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

Instrumentator().instrument(app).expose(app)

@app.on_event("startup")
async def startup_event():
    await dispatcher.start()

@app.on_event("shutdown")
async def shutdown_event():
    await dispatcher.stop()

@app.post("/queue")
async def queue(request: Request):
    """Endpoint to add a request to the queue."""
    try:
        body = await request.body()
        headers = request.headers
        
        backend_request = BackendRequestModel(
            graph=body,
            model_key=headers["model_key"],
            session_id=headers.get("session_id"),
            format=headers["format"],
            zlib=headers.get("zlib", "False").lower() == "true",
            id=str(uuid.uuid4()),
            sent=float(headers.get("sent-timestamp", time.time())),
            api_key=headers.get("ndif-api-key", ""),
        )

        await queue_manager.enqueue(backend_request)

        response = backend_request.create_response(
            status=ResponseModel.JobStatus.APPROVED,
            description="Your job was approved and is waiting to be run.",
            logger=logger,
        )
        
        await response.respond(sio, object_store)
        return {"status": "success", "message": "Request queued successfully"}
        
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