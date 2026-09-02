"""Modeling utilities the controller relies on.

``get_downloaded_models`` is used for status reporting; the remaining helpers
support the model actor when it loads weights onto its assigned GPUs.
"""

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, Tuple

if TYPE_CHECKING:
    from nnsight.schema.response import MetaData

    from .....common.schema.request import BackendRequestModel

logger = logging.getLogger("ndif.modeling")


class LogStream:
    """A stdout stand-in that streams the user's prints back as LOG responses.

    Buffers partial writes and emits one LOG response per complete line so the
    client's status display renders them as they happen. ``flush`` drains any
    trailing text without a newline.
    """

    def __init__(self, request: "BackendRequestModel") -> None:
        from nnsight.schema.response import Status

        self._request = request
        self._status = Status.LOG
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._request.respond(self._status, line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._request.respond(self._status, self._buffer)
            self._buffer = ""


def _downloaded(repo) -> bool:
    """Whether a HF cache repo has real weights (not just a stub .config)."""
    for revision in repo.revisions:
        for file in revision.files:
            if not file.file_name.endswith(".config"):
                return True
    return False


def get_downloaded_models() -> list[str]:
    """Repo ids of models present in the local HuggingFace cache.

    Best-effort: returns an empty list if huggingface_hub isn't available or
    the cache can't be scanned, so status reporting never hard-fails on it.
    """
    try:
        from huggingface_hub import scan_cache_dir

        info = scan_cache_dir()
        return [repo.repo_id for repo in info.repos if _downloaded(repo)]
    except Exception:
        logger.debug("Could not scan the HuggingFace cache", exc_info=True)
        return []


def kill_thread(ident: "int | None", exc_type: type = SystemExit) -> None:
    """Inject ``exc_type`` into the thread ``ident`` (best-effort interruption).

    Uses CPython's async-exception API, which only fires at a bytecode boundary
    in the target thread — it cannot interrupt a native call (a CUDA kernel, a
    large tensor op) already in flight, so a truly runaway execution only stops
    once control returns to Python. No-op if the thread isn't running.
    """
    import ctypes
    import threading

    if ident is None or ident not in {t.ident for t in threading.enumerate()}:
        return

    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_long(ident), ctypes.py_object(exc_type)
    )
    if res > 1:
        # Somehow targeted more than one thread; undo to avoid collateral damage.
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_long(ident), None)


def set_default_gpu(gpu_mem_bytes_by_id: Dict[int, int]) -> None:
    """Pin the process's default CUDA device to the first assigned GPU.

    Must run before any other CUDA call: the CUDA context (~400MiB) is created
    on the current device, so without this it lands on cuda:0 rather than on a
    GPU this replica was actually assigned.
    """
    if not gpu_mem_bytes_by_id:
        return

    import torch

    torch.cuda.set_device(next(iter(gpu_mem_bytes_by_id)))


def remove_accelerate_hooks(module) -> None:
    """Strip accelerate's dispatch hooks from ``module`` and its submodules.

    Dispatching an already-dispatched module (e.g. restoring from cache) would
    otherwise stack hooks on top of the existing ones.
    """
    from accelerate.hooks import remove_hook_from_module

    for _, submodule in module.named_modules():
        if getattr(submodule, "_hf_hook", None) is not None:
            remove_hook_from_module(submodule)


def set_process_limits(gpu_mem_bytes_by_id: Dict[int, int]) -> None:
    """Cap this process' CUDA allocations per GPU to its assigned byte budget.

    For each ``gpu_id -> bytes`` entry, sets the per-process memory fraction so
    the caching allocator refuses to grow past ``bytes`` on that device. The
    fraction is clamped to ``[0, 1]`` since a budget may exceed the card's
    physical memory.
    """
    import torch

    for gpu_id, mem_bytes in gpu_mem_bytes_by_id.items():
        total = torch.cuda.get_device_properties(gpu_id).total_memory
        fraction = min(1.0, max(0.0, mem_bytes / total))
        torch.cuda.set_per_process_memory_fraction(fraction, gpu_id)
        logger.info(
            f"GPU {gpu_id}: limited to {mem_bytes} bytes "
            f"({fraction:.3f} of {total} total)"
        )


def reset_process_limits(gpu_mem_bytes_by_id: Dict[int, int]) -> None:
    """Lift this process' per-GPU allocation caps back to the full card.

    Sets each device's per-process memory fraction to ``1.0`` so a cached model
    isn't held to a stale budget before it is re-dispatched.
    """
    import torch

    for gpu_id in gpu_mem_bytes_by_id:
        torch.cuda.set_per_process_memory_fraction(1.0, gpu_id)


def build_max_memory(gpu_mem_bytes_by_id: Dict[int, int]) -> Dict[int, int]:
    """Build accelerate's ``max_memory`` map for the assigned GPUs.

    Each value is capped at the device's physical memory so accelerate never
    plans a placement that can't physically fit on the card.
    """
    import torch

    max_memory: Dict[int, int] = {}
    for gpu_id, mem_bytes in gpu_mem_bytes_by_id.items():
        total = torch.cuda.get_device_properties(gpu_id).total_memory
        max_memory[gpu_id] = min(mem_bytes, total)
    return max_memory


def verify_device_placement(model: Any, gpu_ids: Any) -> None:
    """Assert every model tensor sits on the requested GPUs and nowhere else.

    Walks the underlying module's parameters and buffers and checks that:
      * no tensor is left on ``cpu`` or the ``meta`` device, and
      * every device used is one of ``gpu_ids``, and
      * every id in ``gpu_ids`` actually holds at least one tensor.

    Raises ``RuntimeError`` describing the first violation encountered.
    """
    expected = {int(gpu_id) for gpu_id in gpu_ids}

    module = getattr(model, "_module", model)

    seen: set[int] = set()
    for name, tensor in [
        *module.named_parameters(),
        *module.named_buffers(),
    ]:
        device = tensor.device

        if device.type == "meta":
            raise RuntimeError(
                f"'{name}' is still on the meta device (weights not dispatched)"
            )
        if device.type != "cuda":
            raise RuntimeError(
                f"'{name}' is on '{device}', expected one of CUDA devices {sorted(expected)}"
            )
        if device.index not in expected:
            raise RuntimeError(
                f"'{name}' is on cuda:{device.index}, "
                f"outside the assigned set {sorted(expected)}"
            )

        seen.add(device.index)

    missing = expected - seen
    if missing:
        raise RuntimeError(
            f"No tensors were placed on assigned GPUs {sorted(missing)}"
        )


def gpu_baselines(gpu_mem_bytes_by_id: Dict[int, int]) -> Dict[int, int]:
    """Reset each assigned GPU's peak-allocation counter and snapshot the current
    allocation, so a request's own footprint can be read afterward.

    Called just before a request executes; pair with ``gpu_peaks``. Best-effort —
    a device that errors is simply left out of the result.
    """
    import torch

    baselines: Dict[int, int] = {}
    for gpu_id in gpu_mem_bytes_by_id:
        try:
            torch.cuda.reset_peak_memory_stats(gpu_id)
            baselines[gpu_id] = torch.cuda.memory_allocated(gpu_id)
        except Exception:
            continue
    return baselines


def gpu_peaks(baselines: Dict[int, int]) -> Dict[int, Tuple[int, int]]:
    """Read each device's peak allocation since ``gpu_baselines``.

    Returns ``{gpu_id: (baseline_bytes, peak_bytes)}`` — the difference is the
    extra memory the just-finished request drove on top of the resident weights.
    """
    import torch

    per_device: Dict[int, Tuple[int, int]] = {}
    for gpu_id, baseline in baselines.items():
        try:
            per_device[gpu_id] = (baseline, torch.cuda.max_memory_allocated(gpu_id))
        except Exception:
            continue
    return per_device


def is_oom(exception: BaseException) -> bool:
    """Whether this failure was the CUDA allocator refusing an allocation.

    ``torch.cuda.OutOfMemoryError`` by type where the build has it; the message
    is the fallback, since a block can wrap or re-raise its own way out of a
    nested call and still be the same failure.

    Here rather than beside ``_is_unrecoverable_cuda_error`` in the actor: that
    one decides whether to restart a replica, this one belongs with the OOM
    measurement it gates, and keeping it a leaf lets the tests reach it without
    the actor's dependencies.
    """
    import torch

    oom_type = getattr(torch.cuda, "OutOfMemoryError", None)
    if oom_type is not None and isinstance(exception, oom_type):
        return True
    return "CUDA out of memory" in str(exception)


# The allocator says how much it could not give you, and on which card. Both are
# rounded to two decimals in whatever unit reads best, which costs a few KB of
# precision on a multi-GB figure and nothing that matters.
_REFUSED = re.compile(r"Tried to allocate ([\d.]+)\s*([KMGT]?i?B)", re.IGNORECASE)
_ON_DEVICE = re.compile(r"GPU (\d+)")
_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}


def _refused_allocation(message: str) -> "Tuple[int | None, int] | None":
    """``(device, bytes)`` of the allocation the allocator turned down.

    ``device`` is ``None`` when the message names no GPU. ``None`` overall when
    the message isn't one of torch's refusals at all.
    """
    refused = _REFUSED.search(message)
    if refused is None:
        return None

    scale = _UNITS.get(refused.group(2).upper())
    if scale is None:
        return None

    on_device = _ON_DEVICE.search(message)
    return (
        int(on_device.group(1)) if on_device else None,
        int(float(refused.group(1)) * scale),
    )


def alloc_shortfall(
    exception: BaseException, gpu_mem_bytes_by_id: Dict[int, int]
) -> "Dict[str, int] | None":
    """Of the allocation the allocator refused, the part that would not fit.

    ``{gpu_id: bytes}``, keyed like ``max_mem_by_gpu`` and with string keys for
    the same reason -- a response crosses the wire as JSON or as ``torch.save``
    bytes, and only JSON stringifies dict keys.

    ``requested - (budget - reserved)``: the part of the refused allocation that
    would not fit in what was left of this actor's assignment. It answers "how
    much do I have to free", which the refused allocation's own size does not --
    asking for 2 GB with 1.9 GB free and asking for it with nothing free are the
    same number and completely different problems.

    Only the requested size is read from the exception; the budget is this
    actor's own and the reserved figure comes from torch. Reserved rather than
    allocated, because the cap is enforced against what the allocator has
    *reserved* from the driver, not against what tensors hold.

    Taken from the message rather than from torch's OOM observer deliberately.
    The observer reports exact integers, but it is out-of-band state that fires
    on refusals torch then recovers from, so it has to be cleared per request and
    can still hand a later failure someone else's number. The message cannot
    desynchronize: it belongs to the exception being reported.

    Approximate by nature -- the allocator frees cached blocks and retries before
    finally failing, so the reserved figure is a moving snapshot, and the message
    rounds. ``None`` when this wasn't a refusal, when the card can't be
    identified, or when there is nothing to compare against.
    """
    import torch

    refusal = _refused_allocation(str(exception))
    if refusal is None:
        return None

    device, requested = refusal
    if device is None:
        # No GPU named. Unambiguous only when this actor holds exactly one.
        if len(gpu_mem_bytes_by_id) != 1:
            return None
        device = next(iter(gpu_mem_bytes_by_id))

    budget = gpu_mem_bytes_by_id.get(device)
    if budget is None:
        return None

    try:
        reserved = torch.cuda.max_memory_reserved(device)
    except Exception:
        return None

    return {str(device): max(requested - (budget - reserved), 0)}


def request_meta(
    per_device: Dict[int, Tuple[int, int]],
    gpu_mem_bytes_by_id: Dict[int, int],
    exec_ms: "float | None",
    exception: "BaseException | None" = None,
) -> "MetaData":
    """What the just-finished request cost, for the response that ends its job.

    Built for a COMPLETED response and for a failed one alike -- the run is over
    either way, and a timeout that peaked at 99% of its headroom explains itself.
    Pass the ``exception`` a failing request died of and, when it was the CUDA
    allocator refusing an allocation, the report also carries
    ``alloc_shortfall_by_gpu`` (see :func:`alloc_shortfall`). Nothing else about
    the report changes, so every terminal response has the same shape but one
    optional key.

    Reuses what the actor already measured for its metrics — ``gpu_peaks`` output
    and the run's wall clock — and shapes it for
    ``nnsight``'s ``ResponseModel.meta_data``:

    Returns nnsight's ``MetaData``, not a dict: the client declares this shape,
    so building it here is what keeps the two repos' idea of the report from
    drifting apart, and a wrong key or a stray integer GPU id fails in this
    actor rather than reaching a user as a differently-shaped payload.

    - ``runtime`` — wall-clock **seconds** (the actor times in ms; this is the
      one place that converts, because the client-facing field is seconds).
    - ``max_mem_by_gpu`` — bytes this request drove *on top of the resident
      weights* (peak minus baseline), per device. Not the device's total usage:
      the weights are the actor's, not the request's.
    - ``max_mem_pct_by_gpu`` — that figure against the headroom the request
      actually had (this actor's assigned bytes on the card, less the weights
      already sitting in them), so 100% means it filled what was left for it.
    - ``max_memory_usage`` — the worst-pressured single device's bytes, for a
      caller that just wants one number.

    GPU keys are **strings**, not ints. A response reaches the client two ways —
    JSON over the socket, or ``torch.save`` bytes when the result rides along —
    and only JSON stringifies dict keys. Emitting strings makes both routes agree,
    so the client isn't parsing a different shape depending on how big its result
    was.

    Best-effort throughout: no CUDA (so no ``per_device``) simply yields zeros and
    empty maps rather than omitting the report.
    """
    by_gpu: Dict[str, int] = {}
    pct_by_gpu: Dict[str, float] = {}
    for gpu_id, (baseline, peak) in per_device.items():
        used = max(peak - baseline, 0)
        by_gpu[str(gpu_id)] = used
        # Headroom is what this actor was assigned on the card minus what its
        # weights already hold. Non-positive means we can't say (the assignment
        # is unknown, or the weights already fill it) -- report 0 rather than
        # dividing by it.
        headroom = gpu_mem_bytes_by_id.get(gpu_id, 0) - baseline
        pct_by_gpu[str(gpu_id)] = (
            round(used / headroom * 100, 2) if headroom > 0 else 0.0
        )

    from nnsight.schema.response import MetaData

    meta = MetaData(
        runtime=round(exec_ms / 1000, 4) if exec_ms is not None else None,
        max_memory_usage=max(by_gpu.values()) if by_gpu else 0,
        max_mem_by_gpu=by_gpu,
        max_mem_pct_by_gpu=pct_by_gpu,
    )

    # Left None on every other outcome, so None is itself the answer to "did
    # this run out of memory".
    if exception is not None and is_oom(exception):
        meta.alloc_shortfall_by_gpu = alloc_shortfall(
            exception, gpu_mem_bytes_by_id
        )

    return meta


def resolve_dtype(dtype: "str | Any | None") -> Any:
    """Resolve a dtype name (e.g. ``"bfloat16"``), a ``torch.dtype``, or ``None``
    to a concrete ``torch.dtype``.

    ``None`` -> ``bfloat16``: the cluster's default and the dtype the controller's
    evaluator estimates model size with, so the actor's load matches the memory
    accounting that placed it.

    A quantization name (``"nf4"``, ``"int8"``, ...) resolves to what that format
    **computes** in, not to its storage width -- there is no ``torch.dtype`` for
    a 4-bit weight, and every caller of this wants the compute dtype anyway: it
    is what user execution autocasts to and what activations come back as. What
    the weights are *held* as never becomes a ``torch.dtype``; it stays the name,
    which is what the loader is handed (see ``BaseModelDeployment.dtype_name``).
    """
    import torch

    from nnsight.modeling.quantization import quantization

    if dtype is None:
        return torch.bfloat16
    if isinstance(dtype, torch.dtype):
        return dtype

    # nnsight owns the table, so a format added there is understood here without
    # a second list to keep in step.
    quantized = quantization(dtype)
    if quantized is not None:
        return resolve_dtype(quantized.compute_dtype)
    # Accepts its own inverse: `str(torch.bfloat16)` is "torch.bfloat16", and a
    # caller shipping a dtype over a socket or a command line reaches for `str`
    # long before it reaches for a prefix strip.
    resolved = getattr(torch, str(dtype).removeprefix("torch."), None)
    if not isinstance(resolved, torch.dtype):
        raise ValueError(f"Unknown torch dtype: {dtype!r}")
    return resolved


_CPU_PICKLE_MODULE = None


def cpu_pickle_module():
    """A ``pickle``-module stand-in whose ``Pickler`` relocates CUDA tensors to
    CPU before serializing, for use as ``torch.save(..., pickle_module=...)``.

    Saving GPU tensors directly serializes larger blobs (they carry CUDA storage
    metadata and don't dedup/compress as well); moving them to CPU first yields
    smaller result uploads. CPU tensors are left untouched. Built lazily and
    memoized so importing this module doesn't require torch.
    """
    global _CPU_PICKLE_MODULE
    if _CPU_PICKLE_MODULE is not None:
        return _CPU_PICKLE_MODULE

    import pickle
    import types

    import torch

    class TensorStoragePickler(pickle.Pickler):
        def reducer_override(self, obj):
            if torch.is_tensor(obj) and obj.device.type != "cpu":
                return obj.detach().to("cpu").__reduce_ex__(pickle.HIGHEST_PROTOCOL)
            return NotImplemented

    module = types.ModuleType("cpu_pickle_module")
    for key, value in pickle.__dict__.items():
        setattr(module, key, value)
    module.Pickler = TensorStoragePickler

    _CPU_PICKLE_MODULE = module
    return _CPU_PICKLE_MODULE
