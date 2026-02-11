"""Local NDIF server — single-model, no infrastructure dependencies.

Provides full nnsight client compatibility (SocketIO + HTTP) without
requiring Redis, MinIO, Ray, or RestrictedPython.
"""

import asyncio
import gc
import logging
import socket
import uuid
from pathlib import Path
from typing import Callable

import socketio
import torch
import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from torch.amp import autocast

from nnsight.intervention.tracing.globals import Globals
from nnsight.intervention.tracing.tracer import Tracer
from nnsight.intervention.tracing.util import wrap_exception
from nnsight.modeling.mixins import RemoteableMixin
from nnsight.schema.request import RequestModel
from nnsight.schema.response import ResponseModel

from .serialization import save_result

logger = logging.getLogger("ndif.local")


# =============================================================================
# Execution
# =============================================================================


class LocalExecutionBackend:
    """Execute nnsight interventions locally without sandboxing.

    Simplified from src/services/ray/src/ray/nn/backend.py — removes the
    Protector wrapper since local mode trusts user code.
    """

    def __init__(self, fn: Callable):
        self.fn = fn

    def __call__(self, tracer: Tracer) -> dict:
        Globals.stack = 0
        Globals.enter()
        try:
            saves = tracer.execute(self.fn)
        except Exception as e:
            raise wrap_exception(e, tracer.info) from None
        finally:
            Globals.exit()
        Globals.saves.clear()
        Globals.stack = 0
        return saves


# =============================================================================
# Port discovery
# =============================================================================


def find_available_port(start_port: int) -> int:
    """Find an available port starting from start_port, incrementing by 1."""
    port = start_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("", port))
                return port
            except OSError:
                port += 1


def check_port_available(port: int) -> bool:
    """Check if a specific port is available."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            return True
        except OSError:
            return False


# =============================================================================
# Server factory
# =============================================================================


class _QueueItem:
    """A request waiting to be processed."""

    __slots__ = ("id", "body", "session_id", "compress")

    def __init__(self, id: str, body: bytes, session_id: str, compress: bool):
        self.id = id
        self.body = body
        self.session_id = session_id
        self.compress = compress


def create_app(
    model_key: str,
    persistent_objects: dict,
    results_dir: Path,
    base_url: str,
) -> FastAPI:
    """Create the FastAPI + SocketIO application.

    Args:
        model_key: The model key this server is serving.
        persistent_objects: Objects from model._remoteable_persistent_objects(),
            needed for request deserialization.
        results_dir: Directory to store result files.
        base_url: Base URL for constructing result download URLs
            (e.g. "http://localhost:8289").
    """
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    sio = socketio.AsyncServer(
        async_mode="asgi",
        max_http_buffer_size=200 * 1024 * 1024,
        ping_timeout=480,
        always_connect=True,
    )

    queue: asyncio.Queue[_QueueItem] = asyncio.Queue()
    results_dir.mkdir(parents=True, exist_ok=True)

    # -- SocketIO events ------------------------------------------------------

    @sio.event
    async def connect(sid, environ):
        pass

    @sio.on("blocking_response")
    async def on_blocking_response(sid, client_session_id, data):
        await sio.emit("blocking_response", data=data, to=client_session_id)

    @sio.on("stream")
    async def on_stream(sid, client_session_id, data, job_id):
        sio.enter_room(sid, job_id)
        await sio.emit("blocking_response", data=data, to=client_session_id)

    @sio.on("stream_upload")
    async def on_stream_upload(sid, data, job_id):
        await sio.emit("stream_upload", data=data, room=job_id)

    # -- HTTP endpoints -------------------------------------------------------

    @app.post("/request")
    async def handle_request(request: Request):
        """Accept an nnsight intervention request.

        Mirrors the production /request endpoint (src/services/api/src/app.py)
        but queues in-memory instead of Redis.
        """
        headers = request.headers
        request_id = headers.get("ndif-request_id")
        if request_id is None:
            request_id = str(uuid.uuid4())

        # Validate model key matches loaded model
        req_model_key = headers.get("nnsight-model-key")
        if req_model_key is not None:
            req_model_key = req_model_key.replace('"revision": "main"', '"revision": null')
            if req_model_key != model_key:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": f"Model mismatch: server is serving {model_key}, "
                        f"but request is for {req_model_key}"
                    },
                )

        session_id = headers.get("ndif-session_id")
        compress_raw = headers.get("nnsight-compress", "true")
        compress = compress_raw.lower() not in ("false", "0", "no", "off")

        body = await request.body()

        await queue.put(_QueueItem(
            id=request_id,
            body=body,
            session_id=session_id,
            compress=compress,
        ))

        response = ResponseModel(
            id=request_id,
            status=ResponseModel.JobStatus.RECEIVED,
            description="Your job has been received and is waiting to be processed.",
            session_id=session_id,
        )
        return response.model_dump(exclude_unset=True)

    @app.get("/results/{result_id}")
    async def get_result(result_id: str):
        """Serve a saved result file."""
        result_path = results_dir / f"{result_id}.pt"
        if not result_path.exists():
            return JSONResponse(status_code=404, content={"error": "Result not found"})
        return FileResponse(result_path, media_type="application/octet-stream")

    @app.get("/ping")
    async def ping():
        return "pong"

    # -- Background worker ----------------------------------------------------

    async def _emit_response(
        session_id: str | None,
        response: ResponseModel,
    ) -> None:
        """Emit a pickled ResponseModel to the client via SocketIO."""
        if session_id:
            await sio.emit("blocking_response", data=response.pickle(), to=session_id)

    async def worker():
        """Process queued requests sequentially."""
        while True:
            item = await queue.get()
            try:
                logger.info(f"{item.id} - Processing request")

                await _emit_response(item.session_id, ResponseModel(
                    id=item.id,
                    status=ResponseModel.JobStatus.RUNNING,
                    description="Your job has started running.",
                    session_id=item.session_id,
                ))

                request_model = RequestModel.deserialize(
                    item.body, persistent_objects, item.compress,
                )

                saves = await asyncio.to_thread(_execute, request_model)

                result_path = results_dir / f"{item.id}.pt"
                save_result(saves, result_path, item.compress)
                result_size = result_path.stat().st_size
                result_url = f"{base_url}/results/{item.id}"

                await _emit_response(item.session_id, ResponseModel(
                    id=item.id,
                    status=ResponseModel.JobStatus.COMPLETED,
                    description="Your job has been completed.",
                    data=(result_url, result_size),
                    session_id=item.session_id,
                ))

                logger.info(f"{item.id} - Completed ({result_size} bytes)")

            except Exception as e:
                logger.exception(f"{item.id} - Error: {e}")
                # Guard emit so a disconnected client doesn't crash the worker
                # (mirrors production: base.py:433-436)
                try:
                    await _emit_response(item.session_id, ResponseModel(
                        id=item.id,
                        status=ResponseModel.JobStatus.ERROR,
                        description=str(e),
                        session_id=item.session_id,
                    ))
                except Exception:
                    logger.exception(f"{item.id} - Failed to emit error response")
            finally:
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                queue.task_done()

    def _execute(request_model: RequestModel) -> dict:
        """Run the intervention graph. Called in a thread."""
        device = "cuda" if torch.cuda.is_available() else "cpu"
        with autocast(device_type=device, dtype=torch.get_default_dtype()):
            return LocalExecutionBackend(request_model.interventions)(
                request_model.tracer
            )

    @app.on_event("startup")
    async def startup():
        asyncio.create_task(worker())

    # Mount Socket.IO under /ws so the full path is /ws/socket.io
    app.mount("/ws", socketio.ASGIApp(sio))

    return app


# =============================================================================
# Entry point
# =============================================================================


def run(
    checkpoint: str,
    host: str = "0.0.0.0",
    port: int | None = None,
    default_port: int = 8289,
    results_dir: Path | None = None,
    dtype: str = "bfloat16",
) -> None:
    """Load a model and start the local NDIF server.

    Args:
        checkpoint: HuggingFace model checkpoint (e.g. "openai-community/gpt2").
        host: Bind address.
        port: Explicit port (fail if unavailable). If None, auto-find from default_port.
        default_port: Starting port for auto-discovery.
        results_dir: Directory for result files. Defaults to /run/user/$UID/ndif/results.
        dtype: Torch dtype for model loading and execution.
    """
    if port is not None:
        if not check_port_available(port):
            raise SystemExit(f"Port {port} is already in use.")
        chosen_port = port
    else:
        chosen_port = find_available_port(default_port)

    if results_dir is None:
        import tempfile
        results_dir = Path(tempfile.gettempdir()) / "ndif" / "results"

    base_url = f"http://{host}:{chosen_port}"
    if host == "0.0.0.0":
        base_url = f"http://localhost:{chosen_port}"

    # Load model
    torch_dtype = getattr(torch, dtype)
    torch.set_default_dtype(torch_dtype)

    logger.info(f"Loading model: {checkpoint}")
    loaded_model_key = _get_model_key(checkpoint)
    model = RemoteableMixin.from_model_key(
        loaded_model_key,
        device_map="auto",
        dispatch=True,
        torch_dtype=torch_dtype,
    )
    model._module.requires_grad_(False)
    persistent_objects = model._remoteable_persistent_objects()

    if torch.cuda.is_available():
        torch.cuda.empty_cache()

    logger.info(f"Model loaded. Starting server on {host}:{chosen_port}")

    app = create_app(
        model_key=loaded_model_key,
        persistent_objects=persistent_objects,
        results_dir=results_dir,
        base_url=base_url,
    )

    uvicorn.run(app, host=host, port=chosen_port, log_level="info")


def _get_model_key(checkpoint: str) -> str:
    """Convert a checkpoint string to a model key.

    Duplicated from cli/lib/util.py:get_model_key to avoid importing ray.
    """
    from nnsight import LanguageModel

    m = LanguageModel(checkpoint, revision=None, dispatch=False)
    return m.to_model_key()
