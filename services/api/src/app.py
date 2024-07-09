import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

import gridfs
import ray
import uvicorn
from bson.objectid import ObjectId
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi_cache import FastAPICache
from fastapi_cache.backends.inmemory import InMemoryBackend
from fastapi_cache.decorator import cache
from fastapi_socketio import SocketManager
from pymongo import MongoClient
from ray import serve

from nnsight.schema.Request import RequestModel

from .api_key import api_key_auth
from .schema import ResponseModel, ResultModel

# Attach to gunicorn logger
logger = logging.getLogger("gunicorn.error")


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

# Init socketio manager app
sm = SocketManager(app=app, mount_location="/ws", logger=logger)

# Init database connection
db_connection = MongoClient(os.environ.get("DATABASE_URL"))

# Init Ray connection
ray.init()


@app.post("/request")
async def request(
    request: RequestModel, api_key=Depends(api_key_auth)
) -> ResponseModel:
    """Endpoint to submit request.

    Args:
        request (RequestModel): _description_

    Returns:
        ResponseModel: _description_
    """
    try:
        # Set the id and time received of request.
        request.received = datetime.now()
        request.id = str(ObjectId())

        # Send to request workers waiting to process requests on the "request" queue.
        # Forget as we don't care about the response.
        serve.get_app_handle("Request").remote(request)

        # Create response object.
        # Log and save to data backend.
        response = (
            ResponseModel(
                id=request.id,
                received=request.received,
                session_id=request.session_id,
                status=ResponseModel.JobStatus.RECEIVED,
                description="Your job has been received and is waiting approval.",
            )
            .log(logger)
            .save(db_connection)
        )

    except Exception as exception:
        # Create exception response object.
        # Log and save to data backend.
        response = (
            ResponseModel(
                id=request.id,
                received=request.received,
                session_id=request.session_id,
                status=ResponseModel.JobStatus.ERROR,
                description=str(exception),
            )
            .log(logger)
            .save(db_connection)
        )

    # Return response.
    return response


async def _blocking_response(response: ResponseModel):
    logger.info(f"Responding to SID: `{response.session_id}`:")
    response.log(logger)

    await sm.emit(
        "blocking_response",
        data=response.model_dump(exclude_defaults=True, exclude_none=True),
        to=response.session_id,
    )


@app.get("/blocking_response/{id}")
async def blocking_response(id: str):
    """Endpoint to have server respond to sid.

    Args:
        id (str): _description_
    """

    response = ResponseModel.load(db_connection, id, result=False)

    await _blocking_response(response)


@app.get("/response/{id}")
async def response(id: str) -> ResponseModel:
    """Endpoint to get latest response for id.

    Args:
        id (str): ID of request/response.

    Returns:
        ResponseModel: Response.
    """

    # Load response from client given id.
    # Don't load result.
    return ResponseModel.load(db_connection, id, result=False)


@app.get("/result/{id}")
async def result(id: str) -> ResultModel:
    """Endpoint to retrieve result for id.

    Args:
        id (str): ID of request/response.

    Returns:
        ResultModel: Result.

    Yields:
        Iterator[ResultModel]: _description_
    """

    # Get cursor to bytes stored in data backend.
    result: gridfs.GridOut = ResultModel.load(db_connection, id, stream=True)

    # Inform client the total size of result in bytes.
    headers = {
        "Content-Length": str(result.length),
    }

    async def stream_gridfs(gridout: gridfs.GridOut):

        for chunk in gridout:
            yield chunk

        ResultModel.delete(db_connection, id, logger=logger)
        ResponseModel.delete(db_connection, id, logger=logger)

    with result:
        return StreamingResponse(
            content=stream_gridfs(result),
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


@app.get("/status", status_code=200)
@cache(expire=120)
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

    for application_status in response.values():

        application_status["status"] = await application_status["status"]

    return response


# FastAPIInstrumentor.instrument_app(app)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=1)
