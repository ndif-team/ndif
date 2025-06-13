import gc
import os
import sys
import time
import traceback
import weakref
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from functools import wraps
from typing import Any, Dict, List, Optional

import boto3
import ray
import socketio
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

from ....logging import load_logger
from ....metrics import (ExecutionTimeMetric, GPUMemMetric,
                         RequestResponseSizeMetric)
from ....schema import (BackendRequestModel, BackendResponseModel,
                        BackendResultModel)
from ...nn.backend import RemoteExecutionBackend
from ...nn.ops import StdoutRedirect
from .util import load_with_cache_deletion_retry

class BaseModelDeployment:

    def __init__(
        self,
        model_key: str,
        cuda_devices:str,
        app: str,
        api_url: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
        execution_timeout: float | None,
        dispatch: bool,
        dtype: str | torch.dtype,
        *args,
        extra_kwargs: Dict[str, Any] = {},
        **kwargs,
    ) -> None:

        super().__init__()
        
        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_devices

        self.api_url = api_url
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key
        
        self.app = app
        self.model_key = model_key
        self.execution_timeout = execution_timeout
        self.dispatch = dispatch
        self.dtype = dtype
        self.extra_kwargs = extra_kwargs

        # Initialize S3 client (either AWS S3 or compatible service like MinIO)
        self.object_store = boto3.client(
            "s3",
            endpoint_url=f"http://{self.object_store_url}",
            aws_access_key_id=self.object_store_access_key,
            aws_secret_access_key=self.object_store_secret_key,
            # Skip verification for local or custom S3 implementations
            verify=False,
            # Set to path style for compatibility with non-AWS S3 implementations
            config=boto3.session.Config(
                signature_version="s3v4", s3={"addressing_style": "path"}
            ),
        )

        self.sio = socketio.SimpleClient(reconnection_attempts=10)

        self.logger = load_logger(
            service_name=str(self.__class__), logger_name="ray.serve"
        )

        self.runtime_context = ray.get_runtime_context()

        if isinstance(dtype, str):

            dtype = getattr(torch, dtype)

        torch.set_default_dtype(torch.bfloat16)

        # if self.cache_evictions is not None:
        #     object_refs = [object_ref_from_id(cache_id) for cache_id in self.cache_evictions]
        #     ray_free(object_refs, local_only=True)


        self.model = self.load_from_disk()

        if dispatch:
            self.model._module.requires_grad_(False)

        torch.cuda.empty_cache()

        self.request: BackendRequestModel
        
        self.thread_pool = ThreadPoolExecutor(max_workers=1)

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

        
        self.logger.info(f"Model loaded from disk in {time.time() - start} seconds on device: {model.device}")
        
        return model
    
    def to_cache(self):
        
        self.model.cpu()

    def from_cache(self, cuda_devices:str, app:str):
        
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
        
        self.logger.info(f"Model loaded from cache in {time.time() - start} seconds on device: {self.model.device}")


    def __call__(self, request: BackendRequestModel) -> None:
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

            #TODO abstract out for distributed execution
            result = self.thread_pool.submit(self.execute, inputs)

            if isinstance(result, Future):
                result = result.result(timeout=self.execution_timeout)

            self.post(result)

        except TimeoutError as e:

            exception = Exception(
                f"Job took longer than timeout: {self.execution_timeout} seconds"
            )

            self.exception(exception)

        except Exception as e:

            self.exception(e)

        finally:

            del request
            del result

            self.cleanup()

    # Ray checks this method and restarts replica if it raises an exception
    def check_health(self):
        pass

    ### ABSTRACT METHODS #################################

    def pre(self) -> RequestModel:
        """Logic to execute before execution."""
        request = self.request.deserialize(self.model)

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
        
        with autocast(device_type="cuda", dtype=torch.get_default_dtype()):

            # For tracking peak GPU usage
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
        ).save(self.object_store)

        self.respond(
            status=BackendResponseModel.JobStatus.COMPLETED,
            description="Your job has been completed.",
        )

        RequestResponseSizeMetric.update(self.request, result.size)
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
        if self.sio.connected:
            self.sio.disconnect()

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
        self.stream_connect()
        return StreamValueModel.deserialize(self.sio.receive(5)[1], "json", True)

    def stream_connect(self):
        """Establishes a websocket connection if one doesn't exist.

        This method ensures that there is an active websocket connection
        before attempting to send or receive data. It:
        1. Checks if a connection exists
        2. If not, creates a new connection with appropriate parameters
        3. Adds a small delay to ensure the connection is fully established

        The connection is established with:
        - WebSocket transport only (no polling fallback)
        - 10-second timeout for connection establishment
        - Job ID included in the connection URL for proper routing of receiving stream data from the user.
        """
        if self.sio.client is None or not self.sio.connected:
            try:
                self.sio.connected = False
                self.sio.connect(
                    f"{self.api_url}?job_id={self.request.id}",
                    socketio_path="/ws/socket.io",
                    transports=["websocket"],
                    wait_timeout=10,
                )
                # Wait for connection to be fully established
                time.sleep(0.1)  # Small delay to ensure connection is ready
            except Exception as e:
                print(f"Error connecting to socketio: {e}")
                time.sleep(1)
                self.stream_connect()

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
        if self.request.session_id is not None:
            self.stream_connect()

        self.request.create_response(**kwargs, logger=self.logger).respond(
            self.sio, self.object_store
        )


class BaseModelDeploymentArgs(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_key: str
    node_name: str

    api_url: str
    object_store_url: str
    object_store_access_key: str
    object_store_secret_key: str

    cached: bool = False

    execution_timeout: float | None = None
    device_map: str | None = "auto"
    dispatch: bool = True
    dtype: str | torch.dtype = "bfloat16"
