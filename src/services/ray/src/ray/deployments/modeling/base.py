import asyncio
import gc
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Set, Literal

import ray
import torch
from accelerate import dispatch_model

from torch.amp import autocast

from transformers.modeling_utils import _get_device_map
from torch.cuda import max_memory_allocated, memory_allocated, reset_peak_memory_stats
from nnsight.modeling.mixins import RemoteableMixin
from nnsight.modeling.mixins.remoteable import StreamTracer
from nnsight.schema.request import RequestModel

from ....logging import set_logger
from ....metrics import (
    ExecutionTimeMetric,
    GPUMemMetric,
    ModelLoadTimeMetric,
    RequestResponseSizeMetric,
)
from ....providers.objectstore import ObjectStoreProvider
from ....providers.socketio import SioProvider
from ....schema import BackendRequestModel, BackendResponseModel, BackendResultModel
from ....types import MODEL_KEY
from ...nn.backend import RemoteExecutionBackend
from ...nn.ops import StdoutRedirect
from ...nn.security.protected_environment import (
    WHITELISTED_MODULES,
    WHITELISTED_MODULES_DESERIALIZATION,
    Protector,
)
from ...nn.security.protected_objects import protect
from .util import kill_thread, load_with_cache_deletion_retry, remove_accelerate_hooks

class BaseModelDeployment:
    def __init__(
        self,
        model_key: MODEL_KEY,
        execution_timeout_seconds: float,
        dispatch: bool,
        dtype: str | torch.dtype,
        target_gpus: List[int] | None = None,
        device_map: str | None = "auto",
        *args,
        extra_kwargs: Dict[str, Any] = {},
        **kwargs,
    ) -> None:
        super().__init__()

        ObjectStoreProvider.connect()
        SioProvider.connect()

        self.model_key = model_key
        self.execution_timeout_seconds = execution_timeout_seconds
        self.dispatch = dispatch
        self.dtype = dtype
        self.extra_kwargs = extra_kwargs
        self.target_gpus = target_gpus or []
        self.cpu_only: bool = device_map == "cpu"

        self.cached = False

        self.logger = set_logger(model_key)

        self.runtime_context = ray.get_runtime_context()

        if isinstance(dtype, str):
            dtype = getattr(torch, dtype)

        torch.set_default_dtype(torch.bfloat16)

        # Set the default CUDA device to the first target GPU BEFORE any CUDA
        # call. This ensures the CUDA context (~400MiB) is created on the
        # target GPU rather than always landing on GPU 0.
        if self.target_gpus:
            torch.cuda.set_device(self.target_gpus[0])

        self.model = self.load_from_disk()

        self.persistent_objects = self.model._remoteable_persistent_objects()

        for key, value in self.persistent_objects.items():
            if isinstance(value, torch.nn.Module):
                self.persistent_objects[key] = protect(value)

        self.execution_protector = Protector(WHITELISTED_MODULES, builtins=True)

        if dispatch:
            self.model._module.requires_grad_(False)

        self._cuda_empty_cache()

        self.request: BackendRequestModel

        self.thread_pool = ThreadPoolExecutor(max_workers=1)

        self.kill_switch = asyncio.Event()
        self.execution_ident = None

        StreamTracer.register(self.stream_send, self.stream_receive)

    def _build_max_memory(self) -> Optional[Dict[int, int]]:
        """Build a max_memory dict that restricts model placement to target GPUs.

        Returns a dict mapping GPU index to max memory in bytes. Non-target GPUs
        get 0 bytes to prevent any allocation. Returns None if no target GPUs
        are set, which lets accelerate use all available GPUs.
        """
        if not self.target_gpus:
            return None

        num_gpus = torch.cuda.device_count()
        target_set = set(self.target_gpus)
        max_memory = {}
        for i in range(num_gpus):
            if i in target_set:
                max_memory[i] = torch.cuda.get_device_properties(i).total_memory
            else:
                max_memory[i] = 0
        return max_memory

    def _verify_device_placement(self, module: torch.nn.Module, source: str):
        """Verify and log that model parameters are on the expected GPUs.

        Args:
            module: The model module to check.
            source: Description of load source (e.g., 'disk', 'cache') for logging.
        """
        devices: Set[str] = set()
        for param in module.parameters():
            devices.add(f"{param.device.type}:{param.device.index}")

        self.logger.info(
            f"Model loaded from {source} on devices: {devices}"
        )

        if self.target_gpus:
            expected = {f"cuda:{gpu}" for gpu in self.target_gpus}
            actual_cuda = {d for d in devices if d.startswith("cuda:")}
            if actual_cuda and not actual_cuda.issubset(expected):
                self.logger.warning(
                    f"Device placement mismatch! Expected GPUs {self.target_gpus}, "
                    f"but model is on {actual_cuda}"
                )

    def load_from_disk(self):
        start = time.time()
        self._cuda_sync()
        self.logger.info(
            f"Loading model from disk for model key {self.model_key} "
            f"targeting GPUs {self.target_gpus}..."
        )

        max_memory = self._build_max_memory()

        device_map = "auto" if not self.cpu_only else "cpu"
        model = load_with_cache_deletion_retry(
            lambda: RemoteableMixin.from_model_key(
                self.model_key,
                device_map=device_map,
                max_memory=max_memory,
                dispatch=self.dispatch,
                torch_dtype=self.dtype,
                **self.extra_kwargs,
            )
        )
        self._cuda_sync()
        load_time = time.time() - start

        ModelLoadTimeMetric.update(load_time, self.model_key, "disk")

        self._verify_device_placement(model._module, "disk")

        self.logger.info(
            f"Model loaded from disk in {load_time} seconds"
        )

        return model

    async def to_cache(self):
        self.logger.info(f"Saving model to cache for model key {self.model_key}...")
        self._cuda_sync()
        await self.cancel()

        remove_accelerate_hooks(self.model._module)

        self.model._module = self.model._module.cpu()
        self._cuda_sync()
        gc.collect()
        self._cuda_empty_cache()

        self.cached = True

    def from_cache(self, target_gpus: List[int]):
        """Restore model from CPU cache onto the specified GPU(s).

        Uses max_memory targeting to ensure the model is placed on exactly
        the requested GPUs, rather than relying on CUDA_VISIBLE_DEVICES
        (which cannot be changed after CUDA context initialization).

        Args:
            target_gpus: List of physical GPU indices to place the model on.
        """
        self.target_gpus = target_gpus

        # Switch default CUDA device to the new target GPU before any CUDA ops
        if self.target_gpus:
            torch.cuda.set_device(self.target_gpus[0])

        self._cuda_sync()
        start = time.time()

        self.logger.info(
            f"Loading model from cache for model key {self.model_key} "
            f"onto GPUs {target_gpus}..."
        )

        max_memory = self._build_max_memory()

        device_map = _get_device_map(self.model._module, "auto" if not self.cpu_only else "cpu", max_memory, None)

        remove_accelerate_hooks(self.model._module)

        self.model._module = dispatch_model(self.model._module, device_map)

        self._cuda_sync()
        gc.collect()
        self._cuda_empty_cache()

        load_time = time.time() - start

        self._verify_device_placement(self.model._module, "cache")

        self.logger.info(
            f"Model loaded from cache in {load_time} seconds"
        )

        ModelLoadTimeMetric.update(load_time, self.model_key, "cache")

        self.cached = False

    async def __call__(self, request: BackendRequestModel) -> None:
        """Executes the model service pipeline:

        1.) Pre-processing
        2.) Execution
        3.) Post-processing
        4.) Cleanup

        Args:
            request (BackendRequestModel): Request.
        """

        if self.cached:
            raise LookupError("Failed to look up actor")

        self.request = request

        try:
            result = None

            inputs = self.pre()

            job_task = asyncio.create_task(asyncio.to_thread(self.execute, inputs))
            kill_task = asyncio.create_task(self.kill_switch.wait())

            done, pending = await asyncio.wait(
                [job_task, kill_task],
                timeout=self.execution_timeout_seconds,
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
                    f"Job took longer than timeout: {self.execution_timeout_seconds} seconds"
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

    def _cuda_sync(self):
        if not self.cpu_only:
            torch.cuda.synchronize()

    def _cuda_empty_cache(self):
        if not self.cpu_only:
            torch.cuda.empty_cache()

    ### ABSTRACT METHODS #################################

    def pre(self) -> RequestModel:

        self.respond(
            status=BackendResponseModel.JobStatus.RUNNING,
            description="Your job has started running.",
        )

        """Logic to execute before execution."""
        with Protector(WHITELISTED_MODULES_DESERIALIZATION):
            request = self.request.deserialize(self.persistent_objects)

        return request

    def execute(self, request: RequestModel) -> Any:
        """Execute request.

        Args:
            request (BackendRequestModel): Request.

        Returns:
            Any: Result.
        """

        self.execution_ident = threading.current_thread().ident

        # Use appropriate device type for autocast
        device_type: Literal["cuda", "cpu"] = "cpu" if self.cpu_only else "cuda"
        
        with autocast(device_type=device_type, dtype=torch.get_default_dtype()):

            if not self.cpu_only:
                reset_peak_memory_stats()
                model_memory = memory_allocated()

            execution_time = time.time()

            # Execute object.
            with StdoutRedirect(self.log):
                result = RemoteExecutionBackend(
                    request.interventions, self.execution_protector
                )(request.tracer)

            execution_time = time.time() - execution_time

            gpu_mem = 0
            if not self.cpu_only:
                gpu_mem = max_memory_allocated() - model_memory


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

        result_object = BackendResultModel(
            id=self.request.id,
            **saves,
        ).save(compress=self.request.compress)

        self.respond(
            status=BackendResponseModel.JobStatus.COMPLETED,
            description="Your job has been completed.",
            data=(result_object.url(), result_object._size),
        )

        RequestResponseSizeMetric.update(self.request, result_object._size)
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

        self.respond(
            status=BackendResponseModel.JobStatus.ERROR,
            description=str(exception),
        )

        # Special handling for CUDA device-side assertion errors
        if "device-side assert triggered" in str(exception):
            self.restart()

    def restart(self):
        """Restarts the Ray serve deployment in response to critical errors.

        This is typically called when encountering CUDA device-side assertion errors
        or other critical failures that require a fresh replica state.
        """
        ray.kill(
            ray.get_actor(f"ModelActor:{self.model_key}", namespace="NDIF"),
            no_restart=False,
        )

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

        self.model._model.zero_grad()
        self.request = None
        gc.collect()
        self._cuda_empty_cache()

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

        response = self.request.create_response(
            BackendResponseModel.JobStatus.STREAM, self.logger, data=data
        )

        SioProvider.emit(
            "stream", data=(self.request.session_id, response.pickle(), self.request.id)
        )

    def stream_receive(self, *args):
        """Receives streaming data from the client.

        This method establishes a websocket connection if needed and waits
        for data from the client. It has a 5-second timeout for receiving data.

        Returns:
            The deserialized data received from the client.
        """
        return SioProvider.sio.receive(5)[1]

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

        try:
            self.request.create_response(**kwargs, logger=self.logger).respond()
        except:
            self.logger.exception("Error responding to client")


@ray.remote(num_cpus=2, num_gpus=0, max_restarts=-1)
class ModelActor(BaseModelDeployment):
    """Ray remote actor for model execution.

    This actor handles the actual model inference and is managed by the
    ModelDeployment class. It inherits from BaseModelDeployment to provide
    the core model functionality.
    """

    pass
