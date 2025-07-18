import os
import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from ray import serve

from nnsight.schema.response import ResponseModel

from .coordination.request_coordinator import RequestCoordinator
from .logging import set_logger
from .providers.objectstore import ObjectStoreProvider
from .providers.socketio import SioProvider
from .schema import BackendRequestModel

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
ObjectStoreProvider.connect()
SioProvider.connect()

coordinator = RequestCoordinator(
    tick_interval=float(os.environ.get("QUEUE_TICK_INTERVAL", 1)),
    max_retries=int(os.environ.get("QUEUE_MAX_RETRIES", 3)),
    max_consecutive_errors=int(os.environ.get("QUEUE_MAX_CONSECUTIVE_ERRORS", 5)),
)

coordinator.start()

Instrumentator().instrument(app).expose(app)

dev_mode = os.environ.get("DEV_MODE", True)  # TODO: default to false


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
        # Create a modified request object with the resolved body
        backend_request = BackendRequestModel.from_request(request)

        logger.debug(f"Creating response for request: {backend_request.id}")
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.QUEUED,
            description="Your job has been recieved by the coordinator and is waiting to be queued.",
            logger=logger,
        ).respond()
        logger.debug(f"Responded to request: {backend_request.id}")

        # Replace the coroutine graph with the actual bytes
        backend_request.request = await backend_request.request

        coordinator.route_request(backend_request)

        logger.debug(f"Enqueued request: {backend_request.id}")

    except Exception as e:
        logger.error(f"Error processing queue request: {str(e)}")
        description = f"{traceback.format_exc()}\n{str(e)}"
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=description,
            logger=logger,
        )
        try:
            response.respond()
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
        raise HTTPException(
            status_code=500, detail=f"Error removing request {request_id}: {e}"
        )


@app.get("/ping", status_code=200)
async def ping():
    """Endpoint to check if the server is online."""
    return "pong"


@app.get("/status", status_code=200)
async def status():
    """Endpoint to check if the server is online."""
    return await serve.get_app_handle("Controller").status.remote()
