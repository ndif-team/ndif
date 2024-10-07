import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from io import BytesIO

import ray
import socketio
import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.decorator import cache
from fastapi_socketio import SocketManager
from minio import Minio
from prometheus_fastapi_instrumentator import Instrumentator
from ray import serve
from urllib3 import HTTPResponse

from nnsight.schema.Response import ResponseModel

from .api_key import api_key_auth
from .logging import load_logger
from .metrics import NDIFGauge
from .schema import BackendRequestModel, BackendResponseModel, BackendResultModel

logger = load_logger(service_name="app", logger_name="gunicorn.error")
gauge = NDIFGauge(service="app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for the FastAPI application.
    
    This function initializes the FastAPICache with an InMemoryBackend
    when the application starts up.

    Args:
        app (FastAPI): The FastAPI application instance.

    Yields:
        None
    """
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

# Init async rabbitmq manager for communication between socketio servers
socketio_manager = socketio.AsyncAioPikaManager(
    url=os.environ.get("RMQ_URL"), logger=logger
)

# Init socketio manager app
sm = SocketManager(
    app=app,
    mount_location="/ws",
    client_manager=socketio_manager,
    logger=logger,
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
    """
    Endpoint to submit a request.

    This function processes the incoming request, stores it in Ray,
    sends it to request workers, and creates a response.

    Args:
        request (BackendRequestModel): The incoming request model.

    Returns:
        BackendResponseModel: The response to the request.
    """
    try:
        object = request.object

        # Put object in ray
        request.object = ray.put(object)

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
        request = request.model_copy()
        request.object = object
        request.save(object_store)

    except Exception as exception:
        # Create exception response object.
        response = request.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=str(exception),
            logger=logger,
            gauge=gauge,
        )

    if not response.blocking():
        response.save(object_store)

    # Return response.
    return response


async def _blocking_response(response: ResponseModel):
    """
    Helper function to emit a blocking response to a specific session. A blocking response is one that the client
    will not be able to send another request until this one is received.

    Args:
        response (ResponseModel): The response to be emitted.
    """
    logger.info(f"Responding to SID: `{response.session_id}`:")
    response.log(logger)

    await sm.emit(
        "blocking_response",
        data=response.model_dump(exclude_defaults=True, exclude_none=True),
        to=response.session_id,
    )


@app.post("/blocking_response")
async def blocking_response(response: BackendResponseModel):
    """
    Endpoint to have server respond to a specific session ID with a blocking response. A blocking response is one that the client
    will not be able to send another request until this one is received.

    Args:
        response (BackendResponseModel): The response to be sent.
    """
    await _blocking_response(response)


@app.get("/response/{id}")
async def response(id: str) -> BackendResponseModel:
    """
    Endpoint to get the latest response for a given ID.

    Args:
        id (str): ID of the request/response.

    Returns:
        BackendResponseModel: The response associated with the given ID.
    """
    # Load response from client given id.
    return BackendResponseModel.load(object_store, id)


@app.get("/result/{id}")
async def result(id: str) -> BackendResultModel:
    """
    Endpoint to retrieve the result for a given ID.

    This function streams the result data and cleans up associated objects
    after successful retrieval.

    Args:
        id (str): ID of the request/response.

    Returns:
        StreamingResponse: A streaming response containing the result data.
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

            # Clean up associated objects
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
    """
    Endpoint to check if the server is online.

    Returns:
        str: "pong" if the server is online.
    """
    return "pong"


@app.get("/stats", status_code=200)
@cache(expire=600)
async def status():
    """
    Endpoint to retrieve the status of running model applications.

    This function collects and returns information about running model
    deployments, including the number of running replicas and their status.

    Returns:
        dict: A dictionary containing status information for each running model application.
    """
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

    # Attempt to retrieve the status for each application
    for key, value in response.items():
        try:
            response[key] = await asyncio.wait_for(value["status"], timeout=4)
        except:
            
            del response[key]

    return response


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=1)
