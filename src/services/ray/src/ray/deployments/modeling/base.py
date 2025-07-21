import asyncio
import gc
import os
import threading
import time
import traceback
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict

import ray
import torch
from accelerate import dispatch_model, infer_auto_device_map
from accelerate.utils import get_balanced_memory
from pydantic import BaseModel, ConfigDict
from ray import serve
from torch.amp import autocast
from torch.cuda import (max_memory_allocated, memory_allocated,
                        reset_peak_memory_stats)

from nnsight.modeling.mixins import RemoteableMixin
from nnsight.schema.request import RequestModel

from ....logging import set_logger
from ....metrics import (ExecutionTimeMetric, GPUMemMetric,
                         ModelLoadTimeMetric, RequestResponseSizeMetric)
from ....providers.objectstore import ObjectStoreProvider
from ....providers.socketio import SioProvider
from ....schema import (BackendRequestModel, BackendResponseModel,
                        BackendResultModel)
from ...nn.backend import RemoteExecutionBackend
from ...nn.ops import StdoutRedirect
from ...nn.security.protected_objects import protect
from ...nn.security.protected_environment import WHITELISTED_MODULES_DESERIALIZATION, Protector
from .util import kill_thread, load_with_cache_deletion_retry


class BaseModelDeployment:

    def __init__(
        self,
        model_key: str,
        cuda_devices: str,
        app: str,
        execution_timeout: float | None,
        dispatch: bool,
        dtype: str | torch.dtype,
        *args,
        extra_kwargs: Dict[str, Any] = {},
        **kwargs,
    ) -> None:

        super().__init__()

        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_devices

        ObjectStoreProvider.connect()

        self.app = app
        self.model_key = model_key
        self.execution_timeout = execution_timeout
        self.dispatch = dispatch
        self.dtype = dtype
        self.extra_kwargs = extra_kwargs

        self.logger = set_logger(app)

        self.runtime_context = ray.get_runtime_context()

        if isinstance(dtype, str):

            dtype = getattr(torch, dtype)

        torch.set_default_dtype(torch.bfloat16)

        self.model = self.load_from_disk()
        self.protected_model = protect(self.model)

        if dispatch:
            self.model._module.requires_grad_(False)

        torch.cuda.empty_cache()

        self.request: BackendRequestModel

        self.thread_pool = ThreadPoolExecutor(max_workers=1)

        self.kill_switch = asyncio.Event()
        self.execution_ident = None

    def load_from_disk(self):

        start = time.time()

        self.logger.info(f"Loading model from disk for model key {self.model_key}...")

        model = load_with_cache_deletion_retry(
            lambda: RemoteableMixin.from_model_key(
                self.model_key,
                device_map="auto",
                dispatch=self.dispatch,
                torch_dtype=self.dtype,
                **self.extra_kwargs,
            )
        )
        
        load_time = time.time() - start
        
        ModelLoadTimeMetric.update(load_time, self.model_key, "disk")

        self.logger.info(
            f"Model loaded from disk in {load_time} seconds on device: {model.device}"
        )

        return model

    async def to_cache(self):

        await self.cancel()

        self.model.cpu()

    def from_cache(self, cuda_devices: str, app: str):

        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_devices

        self.app = app

        start = time.time()

        self.logger.info(f"Loading model from cache for model key {self.model_key}...")

        # Automatically compute balanced memory allocation
        max_memory = get_balanced_memory(self.model._module)

        # Infer an optimal device map based on the computed memory allocation
        device_map = infer_auto_device_map(self.model._module, max_memory=max_memory)

        # Dispatch the model according to the inferred device map

        self.model._module = dispatch_model(self.model._module, device_map=device_map)

        load_time = time.time() - start
        ModelLoadTimeMetric.update(load_time, self.model_key, "cache")

        self.logger.info(
            f"Model loaded from cache in {load_time} seconds on device: {self.model.device}"
        )

    async def __call__(self, request: BackendRequestModel) -> None:
        """Executes the model service pipeline:

        1.) Pre-processing
        2.) Execution
        3.) Post-processing
        4.) Cleanup

        Args:
            request (BackendRequestModel): Request.
        """

        self.request = weakref.proxy(request)

        try:

            result = None

            inputs = self.pre()

            job_task = asyncio.create_task(asyncio.to_thread(self.execute, inputs))
            kill_task = asyncio.create_task(self.kill_switch.wait())

            done, pending = await asyncio.wait(
                [job_task, kill_task],
                timeout=self.execution_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            if job_task in done:
                result = await job_task
            elif kill_task in done:
                kill_thread(self.execution_ident)
                raise Exception("Your job was cancelled or preempted by the server.")
            else:
                kill_thread(self.execution_ident)
                raise Exception(
                    f"Job took longer than timeout: {self.execution_timeout} seconds"
                )

            self.post(result)

        except Exception as e:

            self.exception(e)

        finally:

            del request
            del result

            self.cleanup()

    async def cancel(self):
        if self.execution_ident is not None:
            self.kill_switch.set()

    # Ray checks this method and restarts replica if it raises an exception
    def check_health(self):
        pass

    ### ABSTRACT METHODS #################################

    def pre(self) -> RequestModel:
        """Logic to execute before execution."""
        with Protector(WHITELISTED_MODULES_DESERIALIZATION):
            request = self.request.deserialize(self.protected_model)
        
        if hasattr(request.tracer, "model"):
            request.tracer.model = self.model

        self.respond(
            status=BackendResponseModel.JobStatus.RUNNING,
            description="Your job has started running.",
        )

        return request

    def execute(self, request: RequestModel) -> Any:
        """Execute request.

        Args:
            request (BackendRequestModel): Request.

        Returns:
            Any: Result.
        """

        self.execution_ident = threading.current_thread().ident

        with autocast(device_type="cuda", dtype=torch.get_default_dtype()):

            if torch.cuda.is_available():
                reset_peak_memory_stats()
                model_memory = memory_allocated()

            execution_time = time.time()

            # Execute object.
            with StdoutRedirect(self.log):
                result = RemoteExecutionBackend(request.interventions)(request.tracer)

            execution_time = time.time() - execution_time

            # Compute GPU memory usage
            if torch.cuda.is_available():
                gpu_mem = max_memory_allocated() - model_memory
            else:
                gpu_mem = 0

        return result, gpu_mem, execution_time

    def post(self, result: Any) -> None:
        """Logic to execute after execution with result from `.execute`.

        Args:
            request (BackendRequestModel): Request.
            result (Any): Result.
        """

        saves = result[0]
        gpu_mem: int = result[1]
        execution_time_s: float = result[2]

        result = BackendResultModel(
            id=self.request.id,
           **saves,
        ).save(ObjectStoreProvider.object_store)

        self.respond(
            status=BackendResponseModel.JobStatus.COMPLETED,
            description="Your job has been completed.",
        )

        RequestResponseSizeMetric.update(self.request, result._size)
        GPUMemMetric.update(self.request, gpu_mem)
        ExecutionTimeMetric.update(self.request, execution_time_s)

    def exception(self, exception: Exception) -> None:
        """Handles exceptions that occur during model execution.

        This method processes different types of exceptions and sends appropriate error responses
        back to the client. For NNsight-specific errors, it includes detailed traceback information.
        For other errors, it includes the full exception traceback and message.

        Args:
            exception (Exception): The exception that was raised during __call__.
        """
        # if isinstance(exception, NNsightError):
        #     # Remove traceback limit to get full stack trace
        #     sys.tracebacklimit = None
        #     self.respond(
        #         status=BackendResponseModel.JobStatus.NNSIGHT_ERROR,
        #         description=f"An error has occured during the execution of the intervention graph.\n{exception.traceback_content}",
        #         data={
        #             "err_message": exception.message,
        #             "node_id": exception.node_id,
        #             "traceback": exception.traceback_content,
        #         },
        #     )
        # For non-NNsight errors, include full traceback
        description = traceback.format_exc()
        self.respond(
            status=BackendResponseModel.JobStatus.ERROR,
            description=f"{description}\n{str(exception)}",
        )

        # Special handling for CUDA device-side assertion errors
        if "device-side assert triggered" in str(exception):
            self.restart()

    def restart(self):
        """Restarts the Ray serve deployment in response to critical errors.

        This is typically called when encountering CUDA device-side assertion errors
        or other critical failures that require a fresh replica state.
        """
        serve.get_app_handle(self.app).restart.remote()

    def cleanup(self):
        """Performs cleanup operations after request processing.

        This method:
        1. Disconnects from socketio if connected
        2. Zeros out model gradients
        3. Forces garbage collection
        4. Clears CUDA cache

        This cleanup is important for preventing memory leaks and ensuring
        the replica is ready for the next request.
        """
        self.kill_switch.clear()
        self.execution_ident = None

        SioProvider.disconnect()

        self.model._model.zero_grad()
        gc.collect()
        torch.cuda.empty_cache()

    def log(self, *data):
        """Logs data during model execution.

        This method is used to send log messages back to the client through
        the websocket connection. It joins all provided data into a single string
        and sends it as a LOG status response.

        Args:
            *data: Variable number of arguments to be converted to strings and logged.
        """
        description = "".join([str(_data) for _data in data])
        self.respond(status=BackendResponseModel.JobStatus.LOG, description=description)

    def stream_send(self, data: Any):
        """Sends streaming data back to the client.

        This method is used to send intermediate results or progress updates
        during model execution. It wraps the data in a STREAM status response.

        Args:
            data (Any): The data to stream back to the client.
        """
        self.respond(status=BackendResponseModel.JobStatus.STREAM, data=data)

    def stream_receive(self, *args):
        """Receives streaming data from the client.

        This method establishes a websocket connection if needed and waits
        for data from the client. It has a 5-second timeout for receiving data.

        Returns:
            The deserialized data received from the client.
        """
        return StreamValueModel.deserialize(self.sio.receive(5)[1], "json", True)

    def respond(self, **kwargs) -> None:
        """Sends a response back to the client.

        This method handles sending responses through either websocket
        or object store, depending on whether a session_id exists.

        If session_id exists:
        1. Establishes websocket connection if needed
        2. Sends response through websocket

        If no session_id:
        1. Saves response to object store

        Args:
            **kwargs: Arguments to be passed to create_response, including:
                - status: The job status
                - description: Human-readable status description
                - data: Optional additional data
        """

        self.request.create_response(**kwargs, logger=self.logger).respond()


class BaseModelDeploymentArgs(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_key: str
    node_name: str

    cached: bool = False

    execution_timeout: float | None = None
    device_map: str | None = "auto"
    dispatch: bool = True
    dtype: str | torch.dtype = "bfloat16"
