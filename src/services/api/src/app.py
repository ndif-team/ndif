import os
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict

import socketio
import uvicorn
import boto3
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.decorator import cache
from fastapi_socketio import SocketManager
from prometheus_fastapi_instrumentator import Instrumentator
from slugify import slugify
import requests

from nnsight.schema.response import ResponseModel

from .logging import set_logger

logger = set_logger("API")

from .api_key import api_key_auth
from .metrics import NetworkStatusMetric, TransportLatencyMetric
from .schema import BackendRequestModel, BackendResponseModel, BackendResultModel

@asynccontextmanager
async def lifespan(app: FastAPI):
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
socketio_manager = socketio.AsyncRedisManager(url=os.environ.get("BROKER_URL"))
# Init socketio manager app
sm = SocketManager(
    app=app,
    mount_location="/ws",
    client_manager=socketio_manager,
    max_http_buffer_size=1000000000000000,
    ping_timeout=60,
    always_connect=True,
)

# Init object_store connection
object_store = boto3.client(
    's3',
    endpoint_url=f"http://{os.environ.get('OBJECT_STORE_URL')}",
    aws_access_key_id=os.environ.get("OBJECT_STORE_ACCESS_KEY", "minioadmin"),
    aws_secret_access_key=os.environ.get("OBJECT_STORE_SECRET_KEY", "minioadmin"),
    region_name='us-east-1',
    # Skip verification for local or custom S3 implementations
    verify=False,
    # Set to path style for compatibility with non-AWS S3 implementations
    config=boto3.session.Config(signature_version='s3v4', s3={'addressing_style': 'path'})
)

# Prometheus instrumentation (for metrics)
Instrumentator().instrument(app).expose(app)

from nnsight import __version__
import re

# Extract just the base version number (e.g. 0.4.7 from 0.4.7.dev10+gbcb756d)
SERVER_NNSIGHT_VERSION = re.match(r'^(\d+\.\d+\.\d+)', __version__).group(1)


@app.post("/request")
async def request(
    raw_request: Request
) -> BackendResponseModel:
    """Endpoint to submit request.

    Header:
        - api_key: user api key.

    Request Body:
        raw_request (Request): user request containing the intervention graph.

    Returns:
        BackendResponseModel: reponse to the user request.
    """

    # process the request
    try:
        
        request: BackendRequestModel = BackendRequestModel.from_request(
            raw_request
        )
        
        user_nnsight_version = raw_request.headers.get("nnsight-version", '')
        # Extract just the base version number from user version
        user_base_version = re.match(r'^(\d+\.\d+\.\d+)', user_nnsight_version).group(1)
        
        if user_base_version != SERVER_NNSIGHT_VERSION:
            raise Exception(f"Client version {user_base_version} does not match server version {SERVER_NNSIGHT_VERSION}\nPlease update your nnsight version `pip install --upgrade nnsight`")
        # extract the request data
        

        response = request.create_response(
            status=ResponseModel.JobStatus.RECEIVED,
            description="Your job has been received and is waiting approval.",
            logger=logger,
        )
        
        TransportLatencyMetric.update(request)
        
        NetworkStatusMetric.update(request, raw_request)

        # authenticate api key
        api_key_auth(request)
        
        try:
            body = await request.graph
            headers = dict(raw_request.headers)
            headers["ndif-request_id"] = request.id
            
            logger.info(f"Sending request to queue: {os.environ.get('QUEUE_URL')}/queue")
            queue_response = requests.post(
                f"http://{os.environ.get('QUEUE_URL')}/queue",
                data=body,
                headers=headers,
            )
            
            #if not queue_response.ok:
            #    raise Exception(f"Queue service returned error: {queue_response.status_code} - {queue_response.text}")
                
            logger.info(f"Request sent to queue successfully: {os.environ.get('QUEUE_URL')}/queue")
        except Exception as e:
            description = f"Failed to send request to queue: {e}"
            logger.error(description)
            response = request.create_response(
                status=ResponseModel.JobStatus.ERROR,
                description=description,
                logger=logger,
            )
    except Exception as exception:

        description = f"{traceback.format_exc()}\n{str(exception)}"

        # Create exception response object.
        response = request.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=description,
            logger=logger,
        )

    if not response.blocking:

        response.save(object_store)

    # Return response.
    return response


@sm.on("connect")
async def connect(session_id: str, environ: Dict):
    params = environ.get("QUERY_STRING")
    params = dict(x.split("=") for x in params.split("&"))

    if "job_id" in params:

        await sm.enter_room(session_id, params["job_id"])


@sm.on("blocking_response")
async def blocking_response(session_id: str, client_session_id: str, data: Any):

    await sm.emit("blocking_response", data=data, to=client_session_id)


@sm.on("stream_upload")
async def stream_upload(session_id: str, data: bytes, job_id: str):

    await sm.emit("stream_upload", data=data, room=job_id)


@app.get("/response/{id}")
async def response(id: str) -> BackendResponseModel:
    """Endpoint to get latest response for id.

    Args:
        id (str): ID of request/response.

    Returns:
        BackendResponseModel: Response.
    """

    # Load response from client given id.
    return BackendResponseModel.load(object_store, id)


@app.get("/result/{id}")
async def result(id: str) -> BackendResultModel:
    """Endpoint to retrieve result for id.

    Args:
        id (str): ID of request/response.

    Returns:
        BackendResultModel: Result.

    Yields:
        Iterator[BackendResultModel]: _description_
    """

    # Get cursor to bytes stored in data backend.
    object, content_length = BackendResultModel.load(object_store, id, stream=True)

    # Inform client the total size of result in bytes.
    headers = {
        "Content-length": str(content_length),
    }

    def stream():
        try:
            while True:
                data = object.read(8192)
                if not data:
                    break
                yield data
        finally:
            object.close()

            BackendResultModel.delete(object_store, id)
            BackendResponseModel.delete(object_store, id)
            BackendRequestModel.delete(object_store, id)

    return StreamingResponse(
        content=stream(),
        media_type="application/octet-stream",
        headers=headers,
    )


@app.get("/ping", status_code=200)
async def ping():
    """Endpoint to check if the server is online.
    """
    return "pong"


@app.get("/status", status_code=200)
@cache(expire=60)
async def status():
    return requests.get(
        f"http://{os.environ.get('QUEUE_URL')}/status",
    ).json()

    


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=1)
