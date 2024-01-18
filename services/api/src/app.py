import asyncio
import logging
from datetime import datetime

import gridfs
import socketio
import uvicorn
from bson.objectid import ObjectId
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi_socketio import SocketManager
from pymongo import MongoClient

from nnsight.pydantics import RequestModel

from .celery import celeryconfig
from .celery.tasks import app as celery_app
from .celery.tasks import process_request
from .pydantics import ResponseModel, ResultModel

# Attache to gunicorn logger
logger = logging.getLogger("gunicorn.error")

# Init FastAPI app
app = FastAPI()
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
    url=celeryconfig.broker_url, logger=logger
)
# Init socketio manager app
sm = SocketManager(
    app=app,
    mount_location="/ws",
    client_manager=socketio_manager,
    logger=logger,
    max_http_buffer_size=50000000,
)


@sm.on("blocking_request")
async def blocking_request(sid, request: RequestModel):
    try:
        request = RequestModel(**request)
        request.received = datetime.now()
        request.id = str(ObjectId())
        request.blocking = True
        request.session_id = sid

        process_request.apply_async([request], queue="request").forget()

        response = ResponseModel(
            id=request.id,
            received=request.received,
            blocking=True,
            session_id=request.session_id,
            status=ResponseModel.JobStatus.RECEIVED,
            description="Your job has been received and is waiting approval.",
        ).log(logger)

        await _blocking_response(response)

    except Exception as exception:
        response = ResponseModel(
            id=request.id,
            received=request.received,
            blocking=True,
            session_id=request.session_id,
            status=ResponseModel.JobStatus.ERROR,
            description=str(exception),
        ).log(logger)

        await _blocking_response(response)


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
    client: MongoClient = celery_app.backend._get_connection()

    response = ResponseModel.load(client, id, result=False)

    await _blocking_response(response)


@app.get("/response/{id}")
async def response(id: str) -> ResultModel:
    client: MongoClient = celery_app.backend._get_connection()

    return ResponseModel.load(client, id, result=False)


@app.get("/result/{id}")
async def result(id: str) -> ResultModel:
    client: MongoClient = celery_app.backend._get_connection()

    result: gridfs.GridOut = ResultModel.load(client, id, stream=True)

    headers = {
        "Content-Length": str(result.length),
    }

    async def stream_gridfs(gridout: gridfs.GridOut):
        for chunk in gridout:
            yield chunk

    with result:
        return StreamingResponse(
            content=stream_gridfs(result),
            media_type="application/octet-stream",
            headers=headers,
        )


@app.get("/ping", status_code=200)
async def ping():
    return "pong"


@app.get("/stats", status_code=200)
async def stats():
    stats = celery_app.control.inspect().stats()

    return {
        key: value["custom_info"]
        for key, value in stats.items()
        if "custom_info" in value
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001, workers=1)
