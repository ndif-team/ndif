import os
import gc
import sys
import time
import traceback
import weakref
from concurrent.futures import Future, TimeoutError
from typing import Any, Dict


import torch
from pydantic import ConfigDict
from ray import serve
from torch.amp import autocast
from torch.cuda import max_memory_allocated, memory_allocated, reset_peak_memory_stats

from nnsight.intervention.contexts import RemoteContext
from nnsight.modeling.mixins import RemoteableMixin
from nnsight.schema.request import StreamValueModel
from nnsight.tracing.backends import Backend
from nnsight.tracing.graph import Graph
from nnsight.tracing.protocols import StopProtocol
from nnsight.util import NNsightError

from ...logging import load_logger
from ...metrics import GPUMemMetric, ExecutionTimeMetric
from ...schema import (
    RESULT,
    BackendRequestModel,
    BackendResponseModel,
    BackendResultModel,
)
from ..util import set_cuda_env_var
from . import protocols
from .base import BaseDeployment, BaseDeploymentArgs, threaded


class ExtractionBackend(Backend):
    def __call__(self, graph: Graph) -> RESULT:
        try:
            graph.nodes[-1].execute()
            result = BackendResultModel.from_graph(graph)
        except StopProtocol.StopException:
            result = BackendResultModel.from_graph(graph)
        finally:
            graph.nodes.clear()
            graph.stack.clear()
        return result


class BaseModelDeployment(BaseDeployment):
    def __init__(
        self,
        model_key: str,
        execution_timeout: float | None,
        device_map: str | None,
        dispatch: bool,
        dtype: str | torch.dtype,
        *args,
        extra_kwargs: Dict[str, Any] = {},
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        if os.environ.get("CUDA_VISIBLE_DEVICES", "") == "":
            set_cuda_env_var()

        self.model_key = model_key
        self.execution_timeout = execution_timeout

        if isinstance(dtype, str):
            dtype = getattr(torch, dtype)

        torch.set_default_dtype(torch.bfloat16)

        self.model = RemoteableMixin.from_model_key(
            self.model_key,
            device_map=device_map,
            dispatch=dispatch,
            torch_dtype=dtype,
            **extra_kwargs,
        )

        if dispatch:
            self.model._model.requires_grad_(False)

        torch.cuda.empty_cache()

        self.request: BackendRequestModel

        protocols.LogProtocol.set(lambda *args: self.log(*args))

        RemoteContext.set(self.stream_send, self.stream_receive)

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

            with autocast(device_type="cuda", dtype=torch.get_default_dtype()):
                result = self.execute(inputs)
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

    def check_health(self):
        """Ray checks this method and restarts replica if it raises an exception"""
        pass

    def pre(self) -> Graph:
        """Logic to execute before execution."""
        graph = self.request.deserialize(self.model)
        self.respond(
            status=BackendResponseModel.JobStatus.RUNNING,
            description="Your job has started running.",
        )
        return graph

    def execute(self, graph: Graph) -> Any:
        """Execute request.

        Args:
            request (BackendRequestModel): Request.

        Returns:
            Any: Result.
        """
        if torch.cuda.is_available():
            reset_peak_memory_stats()
            model_memory = memory_allocated()

        execution_time = time.time()
        result = ExtractionBackend()(graph)
        execution_time = time.time() - execution_time

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

        BackendResultModel(
            id=self.request.id,
            result=saves,
        ).save(self.object_store)

        self.respond(
            status=BackendResponseModel.JobStatus.COMPLETED,
            description="Your job has been completed.",
        )

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
        if isinstance(exception, NNsightError):
            sys.tracebacklimit = None
            self.respond(
                status=BackendResponseModel.JobStatus.NNSIGHT_ERROR,
                description=f"An error has occured during the execution of the intervention graph.\n{exception.traceback_content}",
                data={
                    "err_message": exception.message,
                    "node_id": exception.node_id,
                    "traceback": exception.traceback_content,
                },
            )
        else:
            description = traceback.format_exc()
            self.respond(
                status=BackendResponseModel.JobStatus.ERROR,
                description=f"{description}\n{str(exception)}",
            )

        if "device-side assert triggered" in str(exception):
            self.restart()

    def restart(self):
        """Restarts the Ray serve deployment in response to critical errors.
        
        This is typically called when encountering CUDA device-side assertion errors
        or other critical failures that require a fresh replica state.
        """
        app_name = serve.get_replica_context().app_name
        serve.get_app_handle("Controller").restart.remote(app_name)

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
            self.sio.connected = False
            self.sio.connect(
                f"{self.api_url}?job_id={self.request.id}",
                socketio_path="/ws/socket.io",
                transports=["websocket"],
                wait_timeout=10,
            )
            time.sleep(0.1)

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


class BaseModelDeploymentArgs(BaseDeploymentArgs):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_key: str
    execution_timeout: float | None = None
    device_map: str | None = "auto"
    dispatch: bool = True
    dtype: str | torch.dtype = "bfloat16"


class ThreadedModelDeployment(BaseModelDeployment):

    @threaded
    def execute(self, graph: Graph):
        return super().execute(graph)


@serve.deployment(
    ray_actor_options={
        "num_cpus": 2,
    },
    max_ongoing_requests=200, max_queued_requests=200,
    health_check_period_s=10000000000000000000000000000000,
    health_check_timeout_s=12000000000000000000000000000000,
)
class ModelDeployment(ThreadedModelDeployment):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        config = {
            "config_json_string": self.model._model.config.to_json_string(),
            "repo_id": self.model._model.config._name_or_path,
        }
        
        serve.get_app_handle("Controller").set_model_configuration.remote(self.replica_context.app_name, config)


def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
