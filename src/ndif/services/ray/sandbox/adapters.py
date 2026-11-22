"""Replacements for the nnsight and torch methods that assume the model is here.

In the runner the model is weightless and the forward runs on the host, so the
methods a block reaches the model through are replaced by ones that go over the
socket: a model call becomes an ``Interleave`` message, a park goes to the host
untagged, and ``.source``, an ad-hoc module call, ``tracer.cache()`` and
``tracer.barrier()`` park on a request the host answers. Backward passes are
refused, because the graph is on the host.

Importing this module changes nothing. `install` applies the replacements, for
the whole process, and ``runner.py`` calls it once before it serves. Nothing on
the host may call it: there it would turn the real model's ``interleave`` into
one that sends to a socket.
"""

import torch
from greenlet import getcurrent

from nnsight.intervention.barrier import Barrier
from nnsight.intervention.envoy import Envoy
from nnsight.intervention.interleaver import (
    EarlyStopException,
    Event,
    Mediator,
    Pending,
)
from nnsight.intervention.source import SourceEnvoy, SourceNotAvailable
from nnsight.intervention.tracer import InterleavingTracer

from .messages import Call, Interleave, Observe, Source, SourceRefused


def ipc_event(cls, event, location, *rest):
    """Park by handing the host the raw location + current pin, unresolved.

    The base ``Mediator.event`` resolves the occurrence here from the worker's own
    counter. In the split the host owns that counter (one proxy per worker), so a
    park carries the pin and the host resolves and matches it. ``pin`` is the
    worker's ``tracer.iter`` pointer (or None).

    The park is a ``Pending`` rather than a plain tuple because the interleaver
    reads it by name — ``Interleaver._reindex_parked`` builds the parked set from
    ``pending.provider``, and ``Mediator.handle`` matches on ``.provider`` and
    ``.iteration``. A bare tuple carries the same values in the same order and
    still fails on the attribute access.
    """
    worker = getcurrent()
    return worker.parent.switch(
        Pending(event, location, worker.mediator().iteration, *rest)
    )


def ipc_barrier(self):
    """``barrier()`` in a worker: park the arrival for the host to count.

    The base ``Barrier.__call__`` counts arrivals in its own ``_waiting`` list
    and the last one releases the others by switching them directly — all
    inside this process, where the host cannot see it. A released worker's next
    park was stashed on its own mediator (base ``Barrier`` assigns
    ``other.pending``) but never crossed the socket, so the host proxy kept the
    stale barrier park, every post-barrier read went unserved, and the worker's
    saves were silently dropped at the end of the run (#294).

    So in the runner *every* arrival parks over the socket instead, carrying
    what the host needs to do the counting itself (``driver.barrier_arrival``):
    the barrier's identity (``id`` is stable for the request — one Barrier
    object, built by the trace body, shared by its blocks) and the ``n`` it was
    built with. No pin rides along — base ``Mediator.barrier`` carries none
    either — and no release happens here: the host resumes each waiter once the
    round is full, and a resumed worker simply returns from this switch.
    """
    # The same outside-a-trace error the base raises on its _waiting append.
    Mediator.current("tracer.barrier()")
    getcurrent().parent.switch(
        Pending(Event.BARRIER, f"barrier:{id(self)}", None, self.n)
    )


class IPCEnvoy(Envoy):
    """An Envoy that runs the forward pass on the host and interleaves over IPC."""

    def interleave(self, fn, *args, batcher=None, **kwargs):
        # fn is the wrapper method to run on the host (e.g. _call / generate); send
        # its name, not the (unpicklable-here) bound method.
        fn_name = fn if isinstance(fn, str) else fn.__name__
        # The trace's per-invoke interventions are already mediators on this
        # interleaver; run registered edits first — prepend them, mirroring the base
        # Envoy so an edit's swap lands before a trace read this run.
        self.interleaver.mediators[:0] = self._edits
        result = None
        try:
            with self.interleaver:
                # Workers are parked on their first locations; ship each one's park
                # so the host builds a proxy per worker. The per-invoke inputs ride
                # along as raw `invokes` — the pipeline/tokenizer that assemble them
                # into model inputs live on the host, not in this process — then
                # serve the run. Dangling workers are surfaced by the host (Throw).
                mediators = self.interleaver.mediators
                parks = [mediator.pending for mediator in mediators]
                # Each worker's row range in the combined batch, so the host can
                # narrow a read to just that invoke's rows (None = whole batch: an
                # edit or empty invoke).
                batch_groups = [mediator.batch_group for mediator in mediators]
                invokes = batcher.invokes if batcher is not None else []
                self.interleaver.link.send(
                    Interleave(fn_name, parks, batch_groups, invokes, kwargs)
                )
                result = self.interleaver.pump()
        except EarlyStopException:
            pass
        finally:
            self.interleaver.cancel()
        return result

    def __call__(self, *args, hook=False, **kwargs):
        # An ad-hoc module call (e.g. the logit lens): the module lives on the host,
        # so park on a `Call` request with the inputs, run its forward there, and
        # hand the output back. `hook` mirrors Envoy.__call__ (direct forward vs
        # full module).
        return getcurrent().parent.switch(Call(self.path, hook, args, kwargs))


class IPCSource:
    """A ``.source`` in the runner, where the forward that fires lives on the host.

    The base :class:`~nnsight.intervention.source.Source` reads the operation
    names, lines and text off its own ``Compiled``. The runner never has one —
    the host instruments the forward that actually runs — so an ``IPCSource`` is
    built from the host's *description* of it instead: the names, the source
    text, and the line each operation sits on (``None`` when the forward can't
    be sourced). A module's ``.source`` gets the description in the reply to
    its ``Source`` request (``controls.source``); a recursive drill gets the same
    shape served at ``{path}.fn`` (see :func:`ipc_recursive_source`). Each
    ``source.<op>`` is then just a location the interleaver serves, exactly
    like a module ``.output``.
    """

    def __init__(self, envoy: Envoy, path: str, described: "dict | None") -> None:
        self._envoy = envoy
        self._prefix = f"{path}.source"
        if described is None:
            self._names = None
            self._source = ""
            self._lines = {}
        else:
            self._names = described["names"]
            self._source = described["source"]
            self._lines = described["lines"]

    @property
    def names(self) -> tuple:
        # The base Source exposes `.names`; mirror it so a block can list the
        # operations the same way on either path.
        return tuple(self._names or ())

    def __getattr__(self, name: str) -> SourceEnvoy:
        if name.startswith("_"):
            raise AttributeError(name)
        if self._names is not None and name not in self._names:
            available = ", ".join(self._names) or "(none)"
            raise AttributeError(
                f"{self._prefix!r} has no operation {name!r}; available: {available}"
            )
        # A local Source takes the last two off its own Compiled. The runner has
        # no Compiled of the forward that runs, so they came over with the names.
        # They are only read by SourceEnvoy.__repr__, which falls back to the
        # dotted path when the text is empty — so an unsourceable forward still
        # yields a usable envoy.
        return SourceEnvoy(
            self._envoy,
            name,
            f"{self._prefix}.{name}",
            self._source,
            self._lines.get(name, 0),
        )


def ipc_module_source(self: Envoy) -> IPCSource:
    # Round-trip to the host: install source on the module whose forward runs
    # there, and describe it (controls.source).
    described = getcurrent().parent.switch(Source(self.path))
    return IPCSource(self, self.path, described)


def ipc_recursive_source(self: SourceEnvoy) -> IPCSource:
    """``op.source`` in the runner: drill into a called function recursively.

    The base ``SourceEnvoy.source`` resolves the called function from the live
    value flowing through the call — it marks ``interleaver.sourced[path] =
    None``, parks on ``{path}.fn`` until the op fires and ``run_op`` hands the
    callable over, then builds an instrumented copy for the model to run in its
    place. None of that lands in the right process here: the mark and the build
    must sit on the interleaver whose forward runs — the host's. So the runner
    arms the host with a ``Source`` request carrying the **op path**
    (``controls.source``) and parks on ``{path}.fn`` exactly as the
    base does — but what the host serves at that park is a *description*, never
    the callable (#312): it classifies the live value with base nnsight's own
    checks and builds the instrumented copy into its own ``sourced`` at the
    serve moment (``controls.serve_recursive_source``), then ships either
    the one-level reply shape (names/lines/source) or a
    ``SourceRefused(message)`` carrying the base ``SourceNotAvailable``
    wording — raised here from this worker's own frame, exactly as the trusted
    path raises it. Recursive source is ``IPCSource`` all the way down.

    The description is memoized in ``sourced`` under the same per-run scope the
    base property uses (cleared on interleaver entry), so a later access this
    run — a ``tracer.iter`` step — doesn't park on an op that already fired.
    The description holds no tensors, so the runner's check for an edited read
    never sends it back.
    """
    try:
        mediator = Mediator.current(f"{self.path}.source")
    except ValueError:
        # Outside a worker there is no trace to resolve the callable from; the
        # base property's wording, for parity with the trusted path.
        raise SourceNotAvailable(
            "recursive `.source` is only available inside a trace"
        ) from None
    # The *entered* interleaver — the run's — not `self.envoy.interleaver`, the
    # unpickling copy.
    interleaver = mediator.interleaver
    if interleaver.sourced.get(self.path) is None:
        getcurrent().parent.switch(Source(self.path))
        described = Mediator.value(f"{self.path}.fn")
        if isinstance(described, SourceRefused):
            raise SourceNotAvailable(described.message)
        interleaver.sourced[self.path] = described
    return IPCSource(self.envoy, self.path, interleaver.sourced[self.path])


# nnsight's own `tracer.cache`, read before `install` replaces it.
_original_cache = InterleavingTracer.cache


def ipc_cache(self, *args, **kwargs):
    """``tracer.cache()`` in the runner: its values are produced on the host.

    A cache here would never fill — the forward, and so ``Interleaver.handle``, runs
    on the host. So build the ordinary Cache / CacheView (kept a plain nnsight type,
    so the client can deserialize it), then register it and park on an ``Observe``
    request carrying the filter. The host observes on our behalf and ships each kept
    value back as a ``CacheHit``, which :meth:`IPCInterleaver.record` puts into it.
    """
    view = _original_cache(self, *args, **kwargs)
    cache = view._cache
    interleaver = getcurrent().mediator().interleaver
    cache_id = len(interleaver.caches)
    interleaver.caches[cache_id] = cache
    config = {
        "targets": cache.targets,
        "device": cache.device,
        "dtype": cache.dtype,
        "detach": cache.detach,
        "include_output": cache.include_output,
        "include_inputs": cache.include_inputs,
    }
    getcurrent().parent.switch(Observe(cache_id, config))
    return view


# No backward passes on the untrusted path. The model's forward, and the graph a
# backward would walk, run on the host, and a value read from the model arrives
# here as a fresh leaf, so a backward gets no gradients from the model. Left to
# itself it fails opaquely (`with loss.backward(): act.grad` dangles into an
# OutOfOrderError naming a tensor id) or not at all (a bare `loss.backward()`
# leaves every `.grad` None). Refuse it at the call and say why.
NO_BACKWARD = (
    "Backward passes need trusted access. This request is untrusted, so it ran "
    "in a sandbox without the model's computation graph, where gradients do not "
    "flow back into the model. To run backward passes, ask the maintainers of "
    "this NDIF server for trusted access."
)


def refusal():
    # A new function per name, not one shared: torch._dynamo keys its rules by
    # object, gives `torch.autograd.backward` and `torch.autograd.grad` different
    # ones, and asserts when one object turns up under both. transformers imports
    # dynamo mid-request.
    def refuse_backward(*args, **kwargs):
        raise NotImplementedError(NO_BACKWARD)

    return refuse_backward


def install() -> None:
    """Apply the replacements to nnsight and torch, for this whole process."""
    # Every mediator in this process is a worker whose parent lives on the host,
    # so park untagged.
    Mediator.event = classmethod(ipc_event)
    Barrier.__call__ = ipc_barrier
    SourceEnvoy.source = property(ipc_recursive_source)
    # Patch the whole IPCEnvoy class onto the base Envoy. The tracer's root is a
    # model wrapper (TransformersModel -> ... -> Envoy) that inherits interleave
    # from the base, so rebinding this module's Envoy name wouldn't reach it — the
    # overrides have to land on the base class every envoy shares.
    for name, member in vars(IPCEnvoy).items():
        if callable(member) and name in vars(Envoy):
            setattr(Envoy, name, member)
    # `.source` is a property (not caught by the callable patch above): route it
    # to the host-backed IPCSource so source ops work without a local module.
    Envoy.source = property(ipc_module_source)
    InterleavingTracer.cache = ipc_cache
    # `Tensor.backward` is where nnsight's `with loss.backward():` starts too, so
    # this replaces nnsight's patch as well; the other two are torch's direct
    # entry points.
    torch.Tensor.backward = refusal()
    torch.autograd.backward = refusal()
    torch.autograd.grad = refusal()
