"""The model actor the controller deploys.

The controller places a model actor per replica and drives its HOT<->WARM
transitions (``to_cache`` / ``from_cache``); the queue's replica dispatches
requests to it (``run``). This actor loads the weights from the local cache,
dispatches them across the assigned GPUs, and runs the user's traced block
against the model in-process.
"""

import asyncio
import contextlib
import gc
import io
import linecache
import logging
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any, Dict, Optional

import ray
import torch
import zstandard
from accelerate import dispatch_model
from pydantic import BaseModel, ConfigDict
from transformers.integrations.accelerate import _get_device_map

from nnsight.modeling.huggingface import HuggingFaceModel
from nnsight.schema.request import RequestModel
from nnsight.schema.response import Status
from nnsight.tracing.globals import BLOCKS, SOURCES
from nnsight.tracing.tracer import _saves, dec, inc
from nnsight.tracing.util import clean_traceback, filter_traceback

from .....common.metrics import (
    ExecutionTimeMetric,
    GPUMemMetric,
    ModelLoadTimeMetric,
    RequestResponseSizeMetric,
)
from .....common.providers.objectstore import ObjectStoreProvider
from .....common.providers.ray import CachedActorError
from .....common.telemetry import elapsed_ms, event
from .....common.types import MODEL_KEY
from .util import (
    LogStream,
    build_max_memory,
    cpu_pickle_module,
    gpu_baselines,
    gpu_peaks,
    kill_thread,
    remove_accelerate_hooks,
    reset_process_limits,
    resolve_dtype,
    set_default_gpu,
    set_process_limits,
    verify_device_placement,
)

if TYPE_CHECKING:
    from .....common.schema.request import BackendRequestModel

logger = logging.getLogger("ndif.modeling")

# cancel(reason=...) value meaning "this request was not at fault and is still
# runnable — re-queue it rather than failing it". Set when a replica is parked
# (HOT->WARM) out from under a running request. Any other reason, including the
# default of none, is treated as a genuine cancellation.
KILL_REASON_PREEMPTED = "preempted"

# CUDA errors that leave the process's CUDA context permanently broken — every
# subsequent CUDA op raises — so the only recovery is restarting the process.
_UNRECOVERABLE_CUDA_ERRORS = (
    "device-side assert triggered",
    "an illegal memory access was encountered",
)


def _is_unrecoverable_cuda_error(exception: BaseException) -> bool:
    message = str(exception)
    return any(marker in message for marker in _UNRECOVERABLE_CUDA_ERRORS)


class BaseModelDeploymentArgs(BaseModel):
    """Constructor arguments the controller passes to a model actor.

    The controller sets ``model_key`` / ``execution_timeout`` / ``dtype`` and then
    injects the assigned GPU allocation as ``gpu_mem_bytes_by_id`` before
    ``create``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_key: MODEL_KEY
    execution_timeout: Optional[float] = None
    gpu_mem_bytes_by_id: Dict[int, int] = {}
    # torch dtype name (e.g. "bfloat16"). Must match the dtype the controller's
    # evaluator estimated this model's size with, or GPU accounting is wrong.
    dtype: str = "bfloat16"
    # Whether the model may run its own repo code at load (HF trust_remote_code).
    # Set from the trusted flag of the request that deployed it; forwarded through
    # the actor's **kwargs into from_model_key. Must match what the evaluator sized.
    trust_remote_code: bool = False


class BaseModelDeployment:
    """Base model actor. See module docstring."""

    def __init__(
        self,
        model_key: MODEL_KEY,
        execution_timeout: Optional[float] = None,
        gpu_mem_bytes_by_id: Optional[Dict[int, int]] = None,
        dtype: str = "bfloat16",
        **kwargs: Any,
    ) -> None:
        # Install the telemetry sinks in this actor's worker process (each Ray
        # actor is its own process, so they must be set up here, not on the
        # driver): Loki for logs, InfluxDB for metrics.
        from .....common.providers.influx import InfluxProvider
        from .....common.providers.loki import LokiProvider

        LokiProvider.connect()
        InfluxProvider.connect()

        self.model_key = model_key
        self.execution_timeout = execution_timeout
        self.gpu_mem_bytes_by_id = gpu_mem_bytes_by_id or {}
        # Resolve once: used to load the weights and to autocast user execution.
        self.dtype = resolve_dtype(dtype)
        self.kwargs = kwargs
        self.model = None
        self.cached = False

        # Execution runs on a worker thread so run() can time it out or cancel
        # it; kill_switch signals a cancel, execution_ident is that thread's id.
        self.kill_switch = asyncio.Event()
        # Why the kill switch was set. run() needs this to decide whether the
        # request is retryable (PREEMPTED -> the replica is being parked, so
        # re-queue it) or genuinely cancelled (-> tell the user and stop).
        # Deliberately NOT inferred from "the switch fired": a caller that
        # cancels for some other reason must not have its request silently
        # re-queued, which — since re-queues go to the *front* — would re-run it
        # forever. Unset means "genuine cancellation", the safe default.
        self.kill_reason: Optional[str] = None
        self.execution_ident: Optional[int] = None

        # Held so we can kill+restart ourselves after an unrecoverable failure.
        self.runtime_context = ray.get_runtime_context()

        self.model = self.load_from_disk()

        logger.info(f"ModelActor constructed for {model_key}")

    def load_from_disk(self) -> HuggingFaceModel:
        """Load weights from the local cache and dispatch them onto the GPUs.

        Lets accelerate place the model across the assigned GPUs
        (``device_map="balanced"``) within the ``max_memory`` map. A CUDA sync
        ensures every async copy has landed before we verify placement. The
        model is inference-only, so grads are disabled on every parameter.
        """
        started_at = time.time()

        set_default_gpu(self.gpu_mem_bytes_by_id)
        set_process_limits(self.gpu_mem_bytes_by_id)

        max_memory = build_max_memory(self.gpu_mem_bytes_by_id)

        model = HuggingFaceModel.from_model_key(
            self.model_key,
            device_map="balanced",
            max_memory=max_memory,
            dispatch=True,
            torch_dtype=self.dtype,
            **self.kwargs,
        )

        torch.cuda.synchronize()

        model._module.requires_grad_(False)

        verify_device_placement(model, self.gpu_mem_bytes_by_id.keys())

        logger.info(
            f"Loaded {self.model_key} onto GPUs {sorted(self.gpu_mem_bytes_by_id)}"
        )
        ModelLoadTimeMetric.update(
            model_key=self.model_key,
            duration_ms=elapsed_ms(started_at),
            load_type="initial",
            num_gpus=len(self.gpu_mem_bytes_by_id),
        )

        return model

    async def to_cache(self) -> None:
        """Offload weights GPU->CPU (HOT->WARM), freeing the GPU memory.

        Cancels any in-flight execution first (we're about to move the weights
        out from under it), moves the whole module to CPU in one pass, lifts the
        per-GPU allocation caps (``from_cache`` re-applies them), then drops the
        now-dangling CUDA allocations so the freed VRAM is returned to the driver.

        The cancel is flagged ``PREEMPTED`` because the request it interrupts did
        nothing wrong: run() turns that into a ``CachedActorError`` so the queue
        re-queues it, matching what happens when an eviction removes the actor
        outright instead of parking it.
        """
        await self.cancel(KILL_REASON_PREEMPTED)

        self.model._module.to("cpu")

        reset_process_limits(self.gpu_mem_bytes_by_id)

        torch.cuda.synchronize()
        gc.collect()
        torch.cuda.empty_cache()

        self.cached = True

    def from_cache(self, gpu_mem_bytes_by_id: Dict[int, int]) -> None:
        """Restore weights CPU->GPU (WARM->HOT) under the new GPU allocation.

        Re-applies the per-process memory caps for the (possibly reassigned)
        GPUs, computes a balanced device map within that budget, and dispatches
        the already-resident CPU weights onto those GPUs.
        """
        self.gpu_mem_bytes_by_id = gpu_mem_bytes_by_id

        started_at = time.time()

        set_default_gpu(self.gpu_mem_bytes_by_id)
        set_process_limits(self.gpu_mem_bytes_by_id)
        max_memory = build_max_memory(self.gpu_mem_bytes_by_id)

        module = self.model._module

        device_map = _get_device_map(module, "balanced", max_memory, None)
        # Drop the previous dispatch's hooks before re-dispatching, or accelerate
        # stacks a second set on top of the existing ones.
        remove_accelerate_hooks(module)
        dispatch_model(module, device_map=device_map)

        torch.cuda.synchronize()
        gc.collect()
        torch.cuda.empty_cache()

        verify_device_placement(self.model, self.gpu_mem_bytes_by_id.keys())

        self.cached = False

        ModelLoadTimeMetric.update(
            model_key=self.model_key,
            duration_ms=elapsed_ms(started_at),
            load_type="from_cache",
            num_gpus=len(self.gpu_mem_bytes_by_id),
        )

    async def run(self, request: "BackendRequestModel") -> None:
        """Execute a request against this actor's live model, on a worker thread
        raced against ``execution_timeout`` and ``cancel()`` (via ``kill_switch``).

        A template: :meth:`execute` is the worker-thread body (deserialize, run the
        block, return its result blob + deserialize_ms), and :meth:`execution_scope`
        / :meth:`interrupt` / :meth:`format_error` / :meth:`cleanup` are the seams a
        subclass overrides to run the block elsewhere (see the sandbox deployment).
        Everything else — the timeout/cancel race, metrics, event logs,
        cleanup/restart, and the RUNNING/COMPLETED/ERROR responses — is shared.
        """
        # Weights are on CPU (WARM); the queue must treat this like a missing
        # replica. Raise before the try so it propagates as an eviction rather
        # than being turned into a user-facing ERROR response.
        if self.cached:
            raise CachedActorError(f"Model actor {self.model_key} is cached (WARM).")

        request.respond(Status.RUNNING, "Your job has started running.")

        # Snapshot per-GPU allocation to attribute this request's own extra memory
        # footprint after it runs.
        baselines = gpu_baselines(self.gpu_mem_bytes_by_id)

        # deserialize registers the block's source in the global linecache and
        # nnsight memoizes parsed/compiled blocks in process-global SOURCES/BLOCKS
        # (keyed by filename+line), never re-validated — so on a long-lived actor a
        # later request reusing a (filename, line) trace-site would run this
        # request's stale block. Snapshot now and restore after so nothing outlives
        # it. (A no-op for a subclass that deserializes out-of-process.)
        linecache_snapshot = dict(linecache.cache)
        sources_snapshot = dict(SOURCES)
        blocks_snapshot = dict(BLOCKS)
        fatal = False
        url = None
        # Phase timings (ms) for the ExecutionTimeMetric; stay None until measured.
        exec_ms: Optional[float] = None
        deserialize_ms: Optional[float] = None
        upload_ms: Optional[float] = None
        try:
            exec_started = time.time()

            # Apply the request's per-request environment to the host model before
            # it runs (e.g. swap the PEFT adapter it asked for). Host-side and
            # before execute, so it covers both the in-process actor and the
            # sandbox actor (whose runner drives this same self.model); execute()
            # recomputes the persistent objects afterward, so a swapped module tree
            # is reflected. Off the event loop, since a first-time adapter load can
            # block on I/O. Inside the try so a failure (e.g. an unloadable adapter)
            # surfaces to the user as an ERROR with the exact message via
            # format_error, rather than the queue's generic submission error.
            await asyncio.to_thread(self.model._remoteable_set_env, request.env)

            # Run the block on a worker thread, raced against the timeout and the
            # cancel signal. execute() returns (result_blob, deserialize_ms).
            job = asyncio.create_task(asyncio.to_thread(self.execute, request))
            kill = asyncio.create_task(self.kill_switch.wait())
            with self.execution_scope(request):
                done, pending = await asyncio.wait(
                    {job, kill},
                    timeout=self.execution_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            for task in pending:
                task.cancel()

            if job in done:
                data, deserialize_ms = job.result()  # re-raises the block's error
                exec_ms = elapsed_ms(exec_started)
            elif kill in done:
                self.interrupt()
                if self.kill_reason == KILL_REASON_PREEMPTED:
                    # The replica is being parked (HOT->WARM) out from under a
                    # request that was running fine. Fail it the way a *removed*
                    # actor fails — raise so the queue's EVICTED_ERRORS path puts
                    # it back at the front of the line and re-provisions — rather
                    # than erroring a user who did nothing wrong and whose work is
                    # still runnable.
                    #
                    # Parking used to be the harsher of the two eviction outcomes
                    # precisely because it was the tidier one for the cluster: it
                    # keeps the weights in CPU RAM for a fast restore, and killed
                    # the request as a side effect.
                    self.report(request, "preempted", elapsed_ms(exec_started))
                    raise CachedActorError(
                        f"Model actor {self.model_key} was cached (WARM) "
                        "mid-execution."
                    )
                # Any other cancellation is deliberate: somebody wants this
                # request stopped. Terminal, and never re-queued — a re-queue
                # goes to the front of the line, so retrying a request that was
                # cancelled on purpose would re-run it indefinitely.
                request.respond(
                    Status.ERROR,
                    "Your job was cancelled or preempted by the server.",
                )
                self.report(request, "cancelled", elapsed_ms(exec_started))
                return
            else:
                self.interrupt()
                request.respond(
                    Status.ERROR,
                    f"Your job exceeded the execution timeout of "
                    f"{self.execution_timeout}s.",
                )
                self.report(request, "timeout", elapsed_ms(exec_started))
                return

            # Attribute this request's extra GPU footprint (peak - baseline).
            per_device = gpu_peaks(baselines)
            if per_device:
                GPUMemMetric.update(
                    model_key=self.model_key,
                    request_id=request.id,
                    api_key=request.api_key,
                    email=request.email,
                    per_device=per_device,
                )

            upload_started = time.time()
            url = self.upload_bytes(request, data)
            upload_ms = elapsed_ms(upload_started)
        except CachedActorError:
            # Eviction, not a failure of the request. Propagate so the queue
            # re-queues it; turning this into a user-facing ERROR is exactly the
            # bug above. The finally below still restores state and cleans up.
            raise
        except Exception as exception:
            message, fatal = self.format_error(exception)
            request.respond(Status.ERROR, message)
            self.report(
                request,
                "error",
                elapsed_ms(exec_started),
                exception=exception,
                fatal=fatal,
            )
            return
        finally:
            # Undo this request's linecache + nnsight SOURCES/BLOCKS mutations so
            # nothing it registered outlives it, then reclaim per-request state — or
            # restart on a fatal, CUDA-context-corrupting error.
            linecache.cache.clear()
            linecache.cache.update(linecache_snapshot)
            SOURCES.clear()
            SOURCES.update(sources_snapshot)
            BLOCKS.clear()
            BLOCKS.update(blocks_snapshot)
            if fatal:
                self.restart()
            else:
                self.cleanup()

        request.respond(Status.COMPLETED, "Your job has been completed.", data=url)
        self.report(
            request,
            "completed",
            exec_ms,
            deserialize_ms=deserialize_ms,
            upload_ms=upload_ms,
        )

    def execute(self, request: "BackendRequestModel") -> "tuple[bytes, float]":
        """Worker-thread body: deserialize, run the block, torch.save the result.

        Records the running thread's id so run() can interrupt it, deserializes the
        block against this actor's persistent objects (timed, since run() reports
        deserialize_ms), autocasts user-created tensors to the model's dtype,
        collects the ``nnsight.save()``-marked values, and CPU-relocates them into
        the blob run() uploads. Autocast stays here because its state is thread-local.
        """
        self.execution_ident = threading.current_thread().ident

        deserialize_started = time.time()
        persistent_objects = self.model._remoteable_persistent_objects()
        tracer = RequestModel.deserialize(
            request.payload, persistent_objects, compress=request.compress
        )
        deserialize_ms = elapsed_ms(deserialize_started)

        # Autocast tensors the user's code creates to the model's dtype, matching
        # how the weights were loaded. Only meaningful for half precision; a
        # no-op (enabled=False) otherwise, and when there's no CUDA device.
        autocast_enabled = torch.cuda.is_available() and self.dtype in (
            torch.float16,
            torch.bfloat16,
        )
        with torch.autocast(
            device_type="cuda", dtype=self.dtype, enabled=autocast_enabled
        ):
            # Bracket execution in a trace scope (inc/dec), the way the client's
            # Tracer.__exit__ does, so nnsight's save() records into this thread's
            # save set and it's cleared afterward, then collect the values the
            # block marked with nnsight.save() (by identity).
            inc()
            try:
                tracer.execute(tracer.info.code)
                saves = _saves()
                saved = {
                    name: value
                    for name, value in tracer.info.frame.f_locals.items()
                    if id(value) in saves
                }
            finally:
                dec()

        buffer = io.BytesIO()
        torch.save(saved, buffer, pickle_module=cpu_pickle_module())
        return buffer.getvalue(), deserialize_ms

    @contextlib.contextmanager
    def execution_scope(self, request: "BackendRequestModel"):
        """Context wrapping the raced wait. Captures the run's stdout as LOG updates:
        sys.stdout is process-global, so redirecting it on this (event-loop) thread
        covers the worker thread's prints, and the restore runs here — off the thread
        that gets killed on timeout/cancel."""
        log_stream = LogStream(request)
        try:
            with contextlib.redirect_stdout(log_stream):
                yield
        finally:
            log_stream.flush()

    def interrupt(self) -> None:
        """Interrupt the in-flight execution thread (best-effort; see kill_thread)."""
        kill_thread(self.execution_ident)

    def format_error(self, exception: BaseException) -> "tuple[str, bool]":
        """Format an execution failure for the client, and whether it's fatal.

        Drops nnsight internals (clean_traceback) and this actor's own wrapper
        frames, leaving the user's code, and flags an unrecoverable CUDA error so
        run() restarts the replica.
        """
        tb = clean_traceback(exception.__traceback__)
        tb = filter_traceback(tb, lambda frame: frame.f_code.co_filename != __file__)
        message = "".join(traceback.format_exception(type(exception), exception, tb))
        return message, _is_unrecoverable_cuda_error(exception)

    def report(
        self,
        request: "BackendRequestModel",
        status: str,
        exec_ms: Optional[float],
        *,
        exception: Optional[BaseException] = None,
        fatal: bool = False,
        deserialize_ms: Optional[float] = None,
        upload_ms: Optional[float] = None,
    ) -> None:
        """Emit the per-request event log (for non-completed statuses) and the
        execution-time metric."""
        if status == "cancelled":
            event(
                logger, "model execution cancelled", level=logging.WARNING,
                model_key=self.model_key, request_id=request.id,
                api_key=request.api_key, email=request.email,
                stage="running", error_type="cancelled", exec_ms=exec_ms,
            )
        elif status == "preempted":
            # Not a failure: the replica was parked mid-execution and the queue
            # is re-running this request. Logged so the re-run is visible as one
            # request that took two attempts rather than as a mystery latency
            # spike, and so preemption rate is greppable.
            event(
                logger, "model execution preempted; requeued", level=logging.WARNING,
                model_key=self.model_key, request_id=request.id,
                api_key=request.api_key, email=request.email,
                stage="running", error_type="preempted", exec_ms=exec_ms,
            )
        elif status == "timeout":
            event(
                logger, "model execution timed out", level=logging.WARNING,
                model_key=self.model_key, request_id=request.id,
                api_key=request.api_key, email=request.email,
                stage="running", error_type="timeout",
                execution_timeout=self.execution_timeout, exec_ms=exec_ms,
            )
        elif status == "error":
            event(
                logger, "model execution errored", level=logging.ERROR,
                exc_info=exception is not None,
                model_key=self.model_key, request_id=request.id,
                api_key=request.api_key, email=request.email,
                stage="running",
                error_type=type(exception).__name__ if exception else "error",
                fatal=fatal, exec_ms=exec_ms,
            )
        ExecutionTimeMetric.update(
            model_key=self.model_key, request_id=request.id,
            api_key=request.api_key, email=request.email,
            status=status, exec_ms=exec_ms,
            deserialize_ms=deserialize_ms, upload_ms=upload_ms,
        )

    async def cancel(self, reason: Optional[str] = None) -> None:
        """Request cancellation of the in-flight execution, if any.

        Sets the kill switch that run() is waiting on; run() interrupts the
        execution thread and reports the cancellation. No-op when nothing is
        executing.

        ``reason`` tells run() how to treat the request. Pass
        :data:`KILL_REASON_PREEMPTED` when the request is still perfectly
        runnable and should be re-queued (the replica is being parked out from
        under it). Leave it unset for a genuine cancellation — the caller wants
        this request *stopped* — and the user is told it was cancelled.
        """
        if self.execution_ident is not None:
            self.kill_reason = reason
            self.kill_switch.set()

    def restart(self) -> None:
        """Kill this actor and let Ray restart it with a fresh CUDA context.

        Used to recover from failures the process can't come back from (a CUDA
        device-side assert poisons the context for every later op). ``max_restarts=-1``
        means Ray brings the replica back and it reloads the model.
        """
        logger.warning(f"Restarting model actor for {self.model_key}")
        ray.kill(self.runtime_context.current_actor, no_restart=False)

    def cleanup(self) -> None:
        """Reclaim per-request memory once execution is done.

        Runs after every request (success or error) so intermediate activations
        and result tensors don't accumulate across requests on the replica, and
        resets the per-request cancellation state.
        """
        self.kill_switch.clear()
        self.kill_reason = None
        self.execution_ident = None
        self.model.interleaver.cancel()

        torch.cuda.synchronize()
        gc.collect()
        torch.cuda.empty_cache()

    def upload_bytes(self, request: "BackendRequestModel", data: bytes) -> str:
        """Stage a serialized result blob (``torch.save`` output) in the object store.

        The saved values are too big for the redis response channel, so upload the
        blob under the request id and return a presigned url the client downloads
        and injects. Compresses to match the request.
        """
        # Match the request's compression so the client decompresses correctly.
        # zstd level 3: near-lz4 speed with a better ratio — a good balance for
        # the large tensor blobs here (the client unzips under request.compress).
        if request.compress:
            data = zstandard.ZstdCompressor(level=3).compress(data)

        key = f"{request.id}.pt"
        ObjectStoreProvider.put(key, data)

        RequestResponseSizeMetric.update(
            model_key=self.model_key,
            request_id=request.id,
            api_key=request.api_key,
            email=request.email,
            response_bytes=len(data),
            compressed=request.compress,
        )

        return ObjectStoreProvider.presigned_get(key)


@ray.remote(num_cpus=1, max_restarts=-1)
class ModelActor(BaseModelDeployment):
    """The default Ray actor class deployed for a model replica."""

    pass
