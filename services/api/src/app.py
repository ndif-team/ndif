import asyncio
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Dict

import ray
import socketio
import uvicorn
from fastapi import Depends, FastAPI, Path, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.decorator import cache
from fastapi_socketio import SocketManager
from minio import Minio
from prometheus_fastapi_instrumentator import Instrumentator
from ray import serve
from slugify import slugify

from nnsight.schema.request import StreamValueModel
from nnsight.schema.response import ResponseModel

from .api_key import api_key_auth
from .logging import load_logger
from .metrics import NDIFGauge
from .schema import (BackendRequestModel, BackendResponseModel,
                     BackendResultModel)

logger = load_logger(service_name="app", logger_name="gunicorn.error")
gauge = NDIFGauge(service="app")


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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Init async manager for communication between socketio servers
socketio_manager = socketio.AsyncRedisManager(
    url=os.environ.get("BROKER_URL"), logger=logger
)
# Init socketio manager app
sm = SocketManager(
    app=app,
    mount_location="/ws",
    client_manager=socketio_manager,
    logger=logger,
    engineio_logger=logger,
    max_http_buffer_size=1000000000000000
)

# Init object_store connection
object_store = Minio(
    os.environ.get("OBJECT_STORE_URL"),
    access_key=os.environ.get("OBJECT_STORE_ACCESS_KEY", "minioadmin"),
    secret_key=os.environ.get("OBJECT_STORE_SECRET_KEY", "minioadmin"),
    secure=False,
)

# Init Ray connection
ray.init()

# Prometheus instrumentation (for metrics)
Instrumentator().instrument(app).expose(app)


@app.post("/request")
async def request(
    request: BackendRequestModel = Depends(api_key_auth),
) -> BackendResponseModel:
    """Endpoint to submit request.

    Args:
        request (BackendRequestModel): _description_

    Returns:
        BackendResponseModel: _description_
    """
    try:        
        # Send to request workers waiting to process requests on the "request" queue.
        # Forget as we don't care about the response.
        serve.get_app_handle("Request").remote(request)

        # Create response object.
        # Log and save to data backend.
        response = request.create_response(
            status=ResponseModel.JobStatus.RECEIVED,
            description="Your job has been received and is waiting approval.",
            logger=logger,
            gauge=gauge,
        )

        # Back up request object by default (to be deleted on successful completion)
        # request = request.model_copy()
        # request.object = object
        # request.save(object_store)

    except Exception as exception:

        # Create exception response object.
        response = request.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=str(exception),
            logger=logger,
            gauge=gauge,
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
async def stream_upload(session_id: str, data: bytes, job_id:str):
    
    
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
    object = BackendResultModel.load(object_store, id, stream=True)

    # Inform client the total size of result in bytes.
    headers = {
        "Content-Length": object.headers["Content-Length"],
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
            object.release_conn()

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

    Returns:
        _type_: _description_
    """
    return "pong"


@app.get("/stats", status_code=200)
@cache(expire=600)
async def status():

    response = {}

    status = serve.status()

    for application_name, application in status.applications.items():

        if application_name.startswith("Model"):

            deployment = application.deployments["ModelDeployment"]

            num_running_replicas = 0

            for replica_status in deployment.replica_states:

                if replica_status == "RUNNING":

                    num_running_replicas += 1

            if num_running_replicas > 0:

                application_status = serve.get_app_handle(
                    application_name
                ).status.remote()

                response[application_name] = {
                    "num_running_replicas": num_running_replicas,
                    "status": application_status,
                }

    for key, value in list(response.items()):

        try:

            response[key] = await asyncio.wait_for(value["status"], timeout=4)

        except:
            
            del response[key]

    return response


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=1)
