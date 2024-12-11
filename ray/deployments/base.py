import gc
import json
import os
import traceback
import weakref
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from functools import wraps
import sys
from typing import Any, Dict

import ray
import socketio
from nnsight.util import NNsightError
import torch
from minio import Minio
from pydantic import BaseModel, ConfigDict
from torch.amp import autocast
from torch.cuda import (max_memory_allocated, memory_allocated,
                        reset_peak_memory_stats)
from transformers import PreTrainedModel

from nnsight.intervention.contexts import RemoteContext
from nnsight.modeling.mixins import RemoteableMixin
from nnsight.schema.request import StreamValueModel
from nnsight.tracing.backends import Backend
from nnsight.tracing.graph import Graph
from nnsight.tracing.protocols import StopProtocol

from ...logging import load_logger
from ...metrics import NDIFGauge
from ...schema import (RESULT, BackendRequestModel, BackendResponseModel,
                       BackendResultModel)
from ..util import set_cuda_env_var
from . import protocols


class ExtractionBackend(Backend):

    def __call__(self, graph: Graph) -> RESULT:

        try:

            graph.nodes[-1].execute()

            result = BackendResultModel.from_graph(graph)

        except StopProtocol.StopException:

            pass

        finally:

            graph.nodes.clear()
            graph.stack.clear()

        return result


class BaseDeployment:

    def __init__(
        self,
        api_url: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
    ) -> None:
        super().__init__()

        self.api_url = api_url
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key

        self.object_store = Minio(
            self.object_store_url,
            access_key=self.object_store_access_key,
            secret_key=self.object_store_secret_key,
            secure=False,
        )

        self.sio = socketio.SimpleClient(reconnection_attempts=10)

        self.logger = load_logger(
            service_name=str(self.__class__), logger_name="ray.serve"
        )
        self.gauge = NDIFGauge(service="ray")


class BaseDeploymentArgs(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    api_url: str
    object_store_url: str
    object_store_access_key: str
    object_store_secret_key: str


def threaded(method, size: int = 1):

    group = ThreadPoolExecutor(size)

    @wraps(method)
    def inner(*args, **kwargs):

        return group.submit(method, *args, **kwargs)

    return inner


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

    def status(self):

        model: PreTrainedModel = self.model._model

        return {
            "config_json_string": model.config.to_json_string(),
            "repo_id": model.config._name_or_path,
        }

    # Ray checks this method and restarts replica if it raises an exception
    def check_health(self):
        pass

    ### ABSTRACT METHODS #################################

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

        # For tracking peak GPU usage
        if torch.cuda.is_available():   
            reset_peak_memory_stats()
            model_memory = memory_allocated()

        # Execute object.
        result = ExtractionBackend()(graph)

        # Compute GPU memory usage
        if torch.cuda.is_available():
            gpu_mem = max_memory_allocated() - model_memory
        else:
            gpu_mem = 0

        return result, gpu_mem

    def post(self, result: Any) -> None:
        """Logic to execute after execution with result from `.execute`.

        Args:
            request (BackendRequestModel): Request.
            result (Any): Result.
        """

        saves = result[0]
        gpu_mem: int = result[1]

        BackendResultModel(
            id=self.request.id,
            result=saves,
        ).save(self.object_store)

        self.respond(
            status=BackendResponseModel.JobStatus.COMPLETED,
            description="Your job has been completed.",
            gpu_mem=gpu_mem,
        )

    def exception(self, exception: Exception) -> None:
        """Logic to execute of there was an exception.

        Args:
            exception (Exception): Exception.
        """

        if isinstance(exception, NNsightError):
            sys.tracebacklimit = None
            self.respond(
                status=BackendResponseModel.JobStatus.NNSIGHT_ERROR,
                description="An error has occured during the execution of the intervention graph.",
                data={"err_message": exception.message,
                      "node_id": exception.node_id,
                      "traceback": exception.traceback_content}
            )
        else:
            description = traceback.format_exc()

            self.respond(
                status=BackendResponseModel.JobStatus.ERROR, description=description
            )

    def cleanup(self):
        """Logic to execute to clean up memory after execution result is post-processed."""

        if self.sio.connected:

            self.sio.disconnect()

        self.model._model.zero_grad()

        gc.collect()

        torch.cuda.empty_cache()

    def log(self, *data):
        """Logic to execute for logging data during execution.

        Args:
            data (Any): Data to log.
        """

        description = "".join([str(_data) for _data in data])

        self.respond(status=BackendResponseModel.JobStatus.LOG, description=description)

    def stream_send(self, data: Any):
        """Logic to execute to stream data back during execution.

        Args:
            data (Any): Data to stream back.
        """

        self.respond(status=BackendResponseModel.JobStatus.STREAM, data=data)

    def stream_receive(self, *args):

        return StreamValueModel.deserialize(self.sio.receive(5)[1], "json", True)

    def stream_connect(self):

        if self.sio.client is None:

            self.sio.connected = False

            self.sio.connect(
                f"{self.api_url}?job_id={self.request.id}",
                socketio_path="/ws/socket.io",
                transports=["websocket"],
                wait_timeout=10,
            )

    def respond(self, **kwargs) -> None:

        if self.request.session_id is not None:

            self.stream_connect()

        self.request.create_response(
            **kwargs, logger=self.logger, gauge=self.gauge
        ).respond(self.sio, self.object_store)


class BaseModelDeploymentArgs(BaseDeploymentArgs):

    model_key: str
    execution_timeout: float | None = None
    device_map: str | None = "auto"
    dispatch: bool = True
    dtype: str | torch.dtype = torch.bfloat16
