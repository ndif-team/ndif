"""Modeling utilities the controller relies on.

``get_downloaded_models`` is used for status reporting; the remaining helpers
support the model actor when it loads weights onto its assigned GPUs.
"""

import logging
from typing import TYPE_CHECKING, Any, Dict, Tuple

if TYPE_CHECKING:
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
    """A ``pickle``-module stand-in for ``torch.save(..., pickle_module=...)``
    on the result path, overriding plain pickle where plain pickle would either
    bloat the blob or refuse the user's own types.

    **CUDA tensors move to CPU first.** Saving GPU tensors directly serializes
    larger blobs (they carry CUDA storage metadata and don't dedup/compress as
    well); moving them to CPU first yields smaller result uploads. CPU tensors
    are left untouched.

    **User-defined classes survive the trip back.** Requests arrive *by value*:
    nnsight's cloudpickle-based serializer ships a registered or unimportable
    class with its code, so the server can build instances of a library it has
    never installed. Plain pickle on the way out is the asymmetry that breaks
    this — it serializes a class by reference and *verifies* the reference by
    importing the class's module, which doesn't exist here, so saving the very
    instance the block just built dies in ``PicklingError``. Two overrides close
    the gap, chosen by where the class came from:

    - *Arrived by value from the client* (it is in cloudpickle's dynamic-class
      tracker, populated when the request deserialized — all three execution
      paths serialize results in the process that deserialized the request, so
      the tracker is authoritative) → an **unverified by-reference pointer**,
      ``getattr(import_module(mod), name, <by-value twin>)``. The client
      provably had the class — it sent it — so resolving the pointer there
      yields the *original* class and ``isinstance`` against the user's own
      import holds; serializing the class by value instead would hand back a
      reconstructed twin that fails that check. The ``getattr`` default is a
      cloudpickle by-value copy of the class, so a load somewhere the name
      doesn't resolve (a nested class whose dotted qualname cloudpickle
      flattened, a ``__main__`` class read back from a different script) still
      yields a working twin rather than an error.
    - *Born inside the traced block* (blocks cross as source and are exec'd
      here, so the class never existed client-side, is not in the tracker, and
      doesn't resolve by import) → **cloudpickle by value**, reconstructed on
      load via ``pickle.loads``. Twin-class semantics are the best possible:
      there is no original anywhere to point at. Dynamic functions get the same
      fallback. Anything importable on both sides keeps plain pickle's
      by-reference handling.

    The stream stays loadable by a stock client: it references only stdlib,
    torch, and cloudpickle symbols (cloudpickle is already a hard dependency of
    nnsight, and the request path assumes the same cross-version compatibility
    of its reconstructors in the other direction).

    Built lazily and memoized so importing this module doesn't require torch.
    The pickler keys off process-global state only (cloudpickle's tracker), so
    the one module object is safe to share across requests and call sites.
    """
    global _CPU_PICKLE_MODULE
    if _CPU_PICKLE_MODULE is not None:
        return _CPU_PICKLE_MODULE

    import importlib
    import pickle
    import types

    import cloudpickle
    import torch

    try:
        # Classes reconstructed *by value* register here as they are unpickled
        # (`_lookup_class_or_track`), which is exactly the set that arrived by
        # value from the client. A WeakKeyDictionary, so membership is O(1) and
        # nothing is kept alive. Private, hence the guard: if a cloudpickle
        # release moves it, by-value arrivals fall through to the by-value
        # fallback below — results still serialize, only class identity on the
        # client degrades to a twin.
        from cloudpickle.cloudpickle import (
            _DYNAMIC_CLASS_TRACKER_BY_CLASS as _arrived_by_value,
        )
    except ImportError:  # pragma: no cover - depends on cloudpickle internals
        _arrived_by_value = {}

    class _ByValue:
        """A cloudpickle blob of one object, reduced to ``pickle.loads(blob)``.

        The ``getattr`` default in the class pointer has to be an *object in the
        stream* whose reconstruction is the by-value twin; handing ``getattr``
        the class itself would just recurse into the pointer reducer. Never
        leaves this process — only its reduction does.
        """

        __slots__ = ("blob",)

        def __init__(self, obj: Any) -> None:
            self.blob = cloudpickle.dumps(obj)

    def resolves_here(obj: Any) -> bool:
        """Would plain pickle's verified by-reference save succeed for ``obj``?

        The same import-and-compare pickle itself performs, but asked *before*
        the save: pickle raises ``PicklingError`` from inside ``dump`` with no
        way to catch it per-object, so the only place to choose a fallback is
        ahead of time. Importing the module here is no new exposure — plain
        pickle's own save does the identical import today.
        """
        try:
            target = importlib.import_module(obj.__module__)
            for part in obj.__qualname__.split("."):
                target = getattr(target, part)
            return target is obj
        except Exception:
            return False

    def by_value(obj: Any) -> tuple:
        return (pickle.loads, (cloudpickle.dumps(obj),))

    class TensorStoragePickler(pickle.Pickler):
        def _carrier(self, name: str):
            # A bare module object per module name, shared across the dump so
            # pickle's memo collapses repeated pointers into one import. Bare
            # rather than sys.modules[name]: only the *name* must cross, and a
            # synthesized carrier can't drag anything else into the stream.
            carriers = self.__dict__.setdefault("_module_carriers", {})
            if name not in carriers:
                carriers[name] = types.ModuleType(name)
            return carriers[name]

        def reducer_override(self, obj):
            if torch.is_tensor(obj) and obj.device.type != "cpu":
                return obj.detach().to("cpu").__reduce_ex__(pickle.HIGHEST_PROTOCOL)
            if isinstance(obj, types.ModuleType):
                # Both halves of the class pointer are stdlib, so a stock
                # client resolves them with no helper installed: the carrier
                # becomes `import_module(name)` and the pointer wraps it in
                # `getattr`. (Incidentally makes a saved module serializable
                # at all — plain pickle refuses modules outright.)
                return (importlib.import_module, (obj.__name__,))
            if isinstance(obj, _ByValue):
                return (pickle.loads, (obj.blob,))
            if isinstance(obj, type):
                if obj in _arrived_by_value:
                    name = getattr(obj, "__qualname__", None)
                    module = getattr(obj, "__module__", None)
                    # isidentifier() rejects dotted qualnames and `<locals>`;
                    # neither can be resolved by one getattr, so they go by
                    # value instead of emitting a pointer that would resolve
                    # to the wrong object or nothing.
                    if (
                        isinstance(module, str)
                        and module
                        and isinstance(name, str)
                        and name.isidentifier()
                    ):
                        return (getattr, (self._carrier(module), name, _ByValue(obj)))
                    return by_value(obj)
                if resolves_here(obj):
                    return NotImplemented
                return by_value(obj)
            if isinstance(obj, types.FunctionType) and not resolves_here(obj):
                return by_value(obj)
            return NotImplemented

    module = types.ModuleType("cpu_pickle_module")
    for key, value in pickle.__dict__.items():
        setattr(module, key, value)
    module.Pickler = TensorStoragePickler

    _CPU_PICKLE_MODULE = module
    return _CPU_PICKLE_MODULE
