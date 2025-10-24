import os
import traceback
import pickle

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from ray import serve

from nnsight.schema.response import ResponseModel

from .coordination.request_coordinator import RequestCoordinator
from ndif_common.logging import set_logger
from ndif_common.providers.objectstore import ObjectStoreProvider
from ndif_common.providers.socketio import SioProvider
from ndif_common.schema import BackendRequestModel

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

dev_mode = os.environ.get("DEV_MODE", "false").lower() == "true"


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
    backend_request = None

    try:
        # Create a modified request object with the resolved body
        backend_request: BackendRequestModel = pickle.loads(await request.body())

        logger.info(
            f"[REQUEST:{backend_request.id}] Received request for model: {backend_request.model_key}"
        )

        logger.debug(f"Creating response for request: {backend_request.id}")
        response = backend_request.create_response(
            status=ResponseModel.JobStatus.QUEUED,
            description="Your job has been recieved by the coordinator and is waiting to be queued.",
            logger=logger,
        ).respond()
        logger.debug(f"Responded to request: {backend_request.id}")

        coordinator.route_request(backend_request)
        logger.info(
            f"[REQUEST:{backend_request.id}] Successfully routed to coordinator"
        )

        logger.debug(f"Enqueued request: {backend_request.id}")

    except Exception as e:
        logger.exception(f"Error processing queue request: {str(e)}")
        description = f"{traceback.format_exc()}\n{str(e)}"

        if backend_request:
            response = backend_request.create_response(
                status=ResponseModel.JobStatus.ERROR,
                description=description,
                logger=logger,
            )
            try:
                response.respond()
            except:
                logger.exception(f"Failed responding ERROR to user: {description}")
        else:
            logger.exception(
                f"Could not create error response - backend_request was not initialized"
            )

        return {"error": description}


@app.delete("/queue/{request_id}")
async def delete_request(request_id: str):
    """Remove a request from the queue."""
    try:
        if coordinator.remove_task(request_id):
            return {"message": f"Request {request_id} successfully removed from queue"}
        else:
            logger.exception(f"Request {request_id} not found")
            raise HTTPException(
                status_code=404, detail=f"Request {request_id} not found"
            )
    except Exception as e:
        logger.exception(f"Error removing request {request_id}: {e}")
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
