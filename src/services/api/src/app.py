import os
import pickle
import traceback
from typing import Any, Dict

import redis
import socketio
import uvicorn
from fastapi import BackgroundTasks, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_socketio import SocketManager
from prometheus_fastapi_instrumentator import Instrumentator

from nnsight.schema.response import ResponseModel

from .logging import set_logger
from .types import REQUEST_ID, SESSION_ID

logger = set_logger("API")

from .dependencies import validate_request
from .metrics import NetworkStatusMetric
from .providers.objectstore import ObjectStoreProvider
from .schema import BackendRequestModel, BackendResponseModel

# Init FastAPI app
app = FastAPI()

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
    max_http_buffer_size=100_000_000,
    ping_timeout=60,
    always_connect=True,
)

# Init object_store connection
ObjectStoreProvider.connect()

# Prometheus instrumentation (for metrics)
Instrumentator().instrument(app).expose(app)

redis_client = redis.asyncio.Redis.from_url(os.environ.get("BROKER_URL"))


@app.post("/request")
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
            response.save(ObjectStoreProvider.object_store)

        # Run network status metric update in background
        background_tasks.add_task(NetworkStatusMetric.update, backend_request)

        backend_request.request = await backend_request.request

        await redis_client.lpush("queue", pickle.dumps(backend_request))

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
async def connect(session_id: SESSION_ID, environ: Dict):
    params = environ.get("QUERY_STRING")
    params = dict(x.split("=") for x in params.split("&"))

    if "job_id" in params:

        await sm.enter_room(session_id, params["job_id"])


@sm.on("blocking_response")
async def blocking_response(
    session_id: SESSION_ID, client_session_id: SESSION_ID, data: Any
):

    await sm.emit("blocking_response", data=data, to=client_session_id)


@sm.on("stream")
async def stream(
    session_id: SESSION_ID, client_session_id: SESSION_ID, data: bytes, job_id: str
):

    await sm.enter_room(session_id, job_id)

    await blocking_response(session_id, client_session_id, data)


@sm.on("stream_upload")
async def stream_upload(session_id: SESSION_ID, data: bytes, job_id: str):

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
    return BackendResponseModel.load(ObjectStoreProvider.object_store, id)


@app.get("/ping", status_code=200)
async def ping():
    """Endpoint to check if the server is online."""
    return "pong"


@app.get("/state", status_code=200)
async def state():
    """Endpoint to get the state of the dispatcher."""
    if os.environ.get("DEV_MODE", "false") == "true":
        id = str(os.getpid())

        await redis_client.lpush("state", id)
        result = await redis_client.brpop(id)
        return pickle.loads(result[1])



@app.get("/status", status_code=200)
async def status():

    id = str(os.getpid())

    await redis_client.lpush("status", id)
    result = await redis_client.brpop(id)
    return pickle.loads(result[1])


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=1)
