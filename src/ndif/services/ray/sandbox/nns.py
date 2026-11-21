"""nnsight glue for the process sandbox.

The user's traced block is deserialized and executed *inside* the runner process
(nnsight must be importable there). The model, though, lives on the host — so the
intervention code runs here while the forward pass runs there, and the two are
interleaved over the socket.

The interleaver is split across the socket at the mediator level:

* The intervention **workers** (greenlets) run in this process. A worker still
  parks on a location, but instead of resolving the occurrence here it sends the
  raw location plus its current ``tracer.iter`` pin to the host (patched
  ``Mediator.event`` below).
* Each worker's **parent side** — the occurrence counter, the pin, and the
  read/swap matching — lives on the host as one proxy per worker. The host drives
  each worker individually over the socket: ``pump`` switches a worker in on
  ``Resume`` and answers with its next park, throws the host-classified dangling
  error on ``Throw``, and ends on ``Done``. ``tracer.barrier()`` follows the same doctrine: every arrival
  parks over the socket (patched ``Barrier.__call__``) and the *host* counts and
  releases the round, so a released worker's next park always crosses.

So iteration state is tracked per mediator on the host, and a worker's activation
values only cross when the host resumes it (a read) or it swaps (in its park).
Because a read hands back a *copy*, ``pump`` writes each read's value back after
the worker moves past it, so in-place edits (``output[:] = 0``) reach the host.
Two things the runner can't do with a *weightless* module — ``.source`` and an
ad-hoc module call (``IPCEnvoy.__call__``) — become ``Source`` / ``Call`` requests
the host answers.

``IPCCloudUnpickler`` resolves the persistent interleaver to an ``IPCInterleaver``
bound to this request's socket; the model / modules / tokenizer load from this
runner's meta model (``load_meta_model``) — structure, never weights.
"""

import io
import sys
import time
import warnings

import torch
from greenlet import getcurrent

from nnsight.intervention.barrier import Barrier
from nnsight.intervention.cache import Cache
from nnsight.intervention.envoy import Envoy
from nnsight.intervention.interleaver import (
    EarlyStopException,
    Event,
    Interleaver,
    Mediator,
    OutOfOrderError,
    Pending,
)
from nnsight.intervention.serialization import CustomCloudUnpickler
from nnsight.intervention.source import SourceEnvoy, SourceNotAvailable
from nnsight.intervention.tracer import InterleavingTracer
from nnsight.modeling.mixins.remotable import Remotable
from nnsight.tracing.globals import block_scope

from ....common.errors import RequestError
from ....common.schema.request import BackendRequestModel
from ..deployments.modeling.nns import execute_traced_block
from ..deployments.modeling.util import cpu_pickle_module, resolve_dtype
from .link import Link
from .messages import (
    CacheHit,
    Call,
    Dangling,
    Done,
    End,
    Failed,
    Interleave,
    Observe,
    Park,
    Reply,
    Resume,
    Source,
    SourceRefused,
    Stop,
    Throw,
    Writeback,
)

# The runner's link to the host, for the request currently executing. The
# runner handles one request at a time, so a module global is enough.
link = None


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


# Every mediator in this process is a worker whose parent lives on the host, so
# park untagged. (nns is imported only in the runner, so this never hits the host.)
Mediator.event = classmethod(ipc_event)


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


Barrier.__call__ = ipc_barrier


def read_of(park):
    """The read a park represents — ``(location, occurrence)`` — or ``None`` if the
    park isn't a value read. pump tracks these to write in-place edits back."""
    if park is not None and park.event is Event.VALUE:
        return (park.provider, park.iteration)
    return None


class IPCInterleaver(Interleaver):
    """Interleaver whose model side is the host, reached over a socket.

    The workers (greenlets) run here; there are no modules to hook. The host owns
    the parent side and drives each worker by id: :meth:`pump` switches a worker
    in on `Resume` or `Reply` (answering with its next park), throws into one on
    `Throw`, and returns the model's result on `Done`.
    """

    def __init__(self, link) -> None:
        super().__init__()
        self.link = link
        # tracer.cache() caches, by id — the host observes on our behalf and ships
        # each kept value back as a CacheHit for us to record here.
        self.caches: dict[int, Cache] = {}

    def instrument(self, envoy: Envoy) -> None:
        # This process only has meta modules — the forward runs on the host, so
        # hooking here would fire on nothing.
        pass

    def pump(self):
        """Drive the workers from the host until the run ends.

        `Resume` switches a worker in (a read's value, or nothing for a swap) and
        is answered with its next park; the host pushes the worker's pin so
        tracer.iter relaxation stays in lockstep. `Reply` does the same in answer
        to a control request or a barrier park, and leaves the worker's own pin
        standing (see below). `Throw` surfaces a dangling worker. `Done` carries
        the model's result and ends the loop. After a read, the worker's copy of
        the value also goes back (see :meth:`writeback`) so in-place edits reach
        the host.

        ``reads`` tracks each worker's outstanding read (by mediator id), seeded from
        the parks already shipped in the `Interleave` message.

        A worker that calls ``tracer.stop()`` is answered for with `Stop`, and the
        stop is held until the host's `Done`. The host goes on serving the workers
        parked on the same visit and then surfaces the ones left dangling, as
        nnsight's own interleaver does, so this end has to keep answering. And a
        `Done` left unread would be the first thing the session's next trace
        read (#362).
        """
        # A location one of this run's caches wants can be reached on the host at
        # any point; record the (already filtered + moved-to-cpu) value wherever
        # this end is waiting.
        self.link.always = {CacheHit: self.record}
        reads = {
            mediator_id: read
            for mediator_id, mediator in enumerate(self.mediators)
            if (read := read_of(mediator.pending)) is not None
        }
        stopped = None
        while True:
            message = self.link.receive(Resume, Reply, Throw, Done)
            if isinstance(message, Done):
                if stopped is not None:
                    try:
                        raise stopped
                    finally:
                        # The stop's traceback holds this frame; see the same
                        # `del` in nnsight's `Interleaver.handle`.
                        del stopped
                return message.result
            mediator = self.mediators[message.worker]
            if isinstance(message, Throw):
                self.throw(mediator, message.requester, message.kind)
                continue
            # A `Reply` answers a control request or a barrier park, where the
            # host's copy of the pin is stale: relaxation only happens while a
            # model location is served, and the worker may have moved its own
            # pin since its last model park (a `tracer.iter` step). Overwriting
            # it here wound the loop back a step (#296).
            if isinstance(message, Resume):
                mediator.iteration = message.pin
            # The read this message answers, if any — the worker has held its
            # (copied) value and may have edited it in place.
            read = reads.pop(message.worker, None)
            try:
                park = mediator.switch(*message.args)
            except EarlyStopException as stop:
                # A worker halted the run (tracer.stop()); tell the host to
                # unwind the model, and let the stop propagate, as nnsight's
                # does, once the host has finished the run.
                stopped = stop
                self.link.send(Stop())
                continue
            if read is not None:
                self.writeback(message.worker, read, message.args[0])
            if (new_read := read_of(park)) is not None:
                reads[message.worker] = new_read
            self.link.send(Park(message.worker, park))

    def record(self, hit) -> None:
        """Put one value the host observed into the cache it belongs to."""
        self.caches[hit.cache]._record(hit.path, hit.key, hit.value)

    def writeback(self, mediator_id, read, value):
        """Send a read's (maybe-edited) value back for the host to write into its
        own, and wait for the host's reply.

        A read hands the worker a *copy* of the model's value (it crossed the
        socket), so an in-place edit — ``output[:] = 0`` — never reaches the host on
        its own. The read left the host parked at that location, so writing back now
        applies any edit before the model moves on. We always write back (any
        tensor may have been edited, and we can't tell from here).

        A `Writeback` control request, not a swap: a swap replaces the host's
        objects with these copies, and the forward's other references to the
        originals then hold something else than the module ran on (see
        ``controls.write_back``).
        """
        # TODO: only the value is sent back — eproperty write-back transforms
        # attached to the read are silently dropped on the sandbox path.
        location, _ = read
        self.link.request(Park(mediator_id, Writeback(location, value)), Reply)

    def throw(self, mediator, requester, kind):
        """End a worker still parked after the run, as the host classified it.

        Mirrors ``Interleaver.check_dangling_mediators`` / ``dangling_unwind``:
        ``kind`` is the host's classification (``check_dangling``), a `Dangling`:

        * ``BARRIER`` — the barrier was never going to release (fewer blocks
          reached it than it was built for); the base's ValueError, raised out
          of the worker's own frame, same wording as the trusted path.
        * ``ITER`` — a ``tracer.iter`` loop outran the model; unwound and
          warned about, its reached iterations kept.
        * ``OUT_OF_ORDER`` — a real error: the model already ran past the
          requested location; propagates, failing the run.
        """
        if kind is Dangling.BARRIER:
            mediator.worker.throw(
                ValueError(
                    "A barrier was never reached by every block it waits for; "
                    "check the count it was created with"
                )
            )
            return
        error = OutOfOrderError(
            f"'{requester}' was requested but the model already ran past it"
        )
        if kind is Dangling.ITER:
            try:
                mediator.worker.throw(error)
            except OutOfOrderError:
                warnings.warn(
                    f"'{requester}' was never reached: the model ran fewer "
                    f"iterations than the loop requested. Values from reached "
                    f"iterations are kept."
                )
        else:
            mediator.worker.throw(error)


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


class IPCCloudUnpickler(CustomCloudUnpickler):
    """nnsight unpickler that resolves the interleaver to an IPC one.

    Passed to ``BackendRequestModel.deserialize(..., unpickler=IPCCloudUnpickler)``,
    which instantiates it as ``IPCCloudUnpickler(file, persistent_objects)`` — here
    the map ``load_meta_model`` built from this runner's meta model, so
    ``Module:<path>``, ``Tokenizer`` and ``Pipeline`` ids resolve to real
    (weightless) objects in this process. Unlike the base class an unknown id is not
    an error: it resolves to ``None``, since the payload can mention objects only the
    host has (and a runner launched without a model key has an empty map, which is
    every id). Resolving to ``None`` is safe precisely because nothing local reads
    a module — every read is a socket round trip — so an id the meta tree lacks
    costs an ``AttributeError`` in the block rather than
    ``BackendRequestModel.deserialize``'s ``ArchitectureMismatchError``, which this
    path has never raised.
    """

    def persistent_load(self, pid):
        # One interleaver is shared across the envoy tree; bind it to this
        # request's socket. Checked before the map so the meta model's own
        # interleaver (also keyed "Interleaver") can't shadow the IPC one. The model
        # weights themselves are never in this process — a resolved module is the
        # meta build's, and reads of its activations still cross the socket.
        if pid == "Interleaver":
            return IPCInterleaver(link)
        elif pid in self.persistent_objects:
            return self.persistent_objects[pid]

        return None


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
    The generic read writeback ships the description back too; harmless, since it
    holds no tensors.
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


SourceEnvoy.source = property(ipc_recursive_source)


# Patch the whole IPCEnvoy class onto the base Envoy. The tracer's root is a model
# wrapper (TransformersModel -> ... -> Envoy) that inherits interleave from the
# base, so rebinding this module's Envoy name wouldn't reach it — the overrides
# have to land on the base class every envoy shares.
for _name, _member in vars(IPCEnvoy).items():
    if callable(_member) and _name in vars(Envoy):
        setattr(Envoy, _name, _member)

# `.source` is a property (not caught by the callable patch above): route it to the
# host-backed IPCSource so source ops work without a local module.
Envoy.source = property(ipc_module_source)


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


_original_cache = InterleavingTracer.cache
InterleavingTracer.cache = ipc_cache


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


# `Tensor.backward` is where nnsight's `with loss.backward():` starts too, so this
# replaces nnsight's patch as well; the other two are torch's direct entry points.
torch.Tensor.backward = refusal()
torch.autograd.backward = refusal()
torch.autograd.grad = refusal()

# This runner's weightless model, and the persistent-id map built from it. The
# map is what `IPCCloudUnpickler` resolves a payload's ids against; the model is
# kept because it is the thing that produced the map.
META_MODEL = None
PERSISTENT_OBJECTS = {}


def load_meta_model(model_key: str) -> None:
    """Build this runner's meta model and register its persistent-id map.

    Called once at runner start (``Runner.__init__``, before the socket binds), so
    every request this process serves resolves ``Module:<path>`` / ``Tokenizer`` /
    ``Pipeline`` against a tree with the same paths as the host's real model. The
    build is undispatched — structure only, no weights — and its cost is paid per
    runner, i.e. inside ``spawn``'s readiness window rather than in a request.

    Nothing here dispatches: ``from_model_key`` defaults to the meta build, and the
    tracer the runner later deserializes is a *different* wrapper object whose
    ``__setstate__`` marks it dispatched, so no ``interleave`` in this process can
    decide it needs real weights. This object is only ever read for its map.

    ``trust_remote_code`` is in the key, so this build is the actor's build.

    No hub round trip is required: the actor loads its model before it opens its
    pool, so the config and tokenizer this reads are already in the shared cache,
    and huggingface_hub serves a cached file even when its HEAD call fails — which
    is what happens here for a gated repo, since a runner deliberately inherits no
    `HF_TOKEN`.
    """
    global META_MODEL

    # `from_model_key` resolves the wrapper class from the key itself, so this is
    # the same build the actor does (base.py) minus the weights -- and it is right
    # for a non-HuggingFace wrapper too.
    META_MODEL = Remotable.from_model_key(model_key)
    PERSISTENT_OBJECTS.update(META_MODEL._remoteable_persistent_objects())


def set_env(env: "dict | None") -> None:
    """Apply a request's environment to this runner's meta model, like the host.

    The host applies the same dict to the loaded model before the block runs
    (``base.py``), and for a PEFT adapter that *changes the module tree* — every
    path gains a ``base_model.model`` prefix. Without this the runner's map would
    describe the unadapted tree and every ``Module:`` id in an adapted request
    would miss it, so the runner would fall back to the pre-meta-model behaviour
    for exactly the requests that ask for something unusual.

    The adapter's weights land on meta parameters, which torch warns about once
    per tensor and is precisely what is wanted here: the paths are the point, the
    values are the host's. The warnings are swallowed because this process's
    stderr is the client's log.

    A no-op when the model already matches (the common case: no adapter at all),
    so an ordinary request pays a dict comparison.

    Best-effort, deliberately: the map is an optimization, and a runner that
    cannot follow the host into some environment still runs the request correctly
    with those ids resolving to ``None`` — which is all any of them did before
    meta models. Failing the request instead would turn a missing optional
    package in the *runner* into a user-facing error for a request the actor
    itself accepted.
    """
    if META_MODEL is None:
        return

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            META_MODEL._remoteable_set_env(env)
    except Exception as error:
        # The runner's stderr, not the client's: this is the actor's business.
        print(f"runner: could not apply the request env: {error!r}", file=sys.__stderr__)
        return

    PERSISTENT_OBJECTS.clear()
    PERSISTENT_OBJECTS.update(META_MODEL._remoteable_persistent_objects())


def run(conn, blob: bytes, compress: bool = False, dtype: str = "float32",
        seed: "int | None" = None, env: "dict | None" = None) -> None:
    """Deserialize the request in this process and execute it; interleave over IPC.

    Runs as the runner's user code: ``conn`` is the socket to the host. Model calls
    hit ``IPCEnvoy.interleave`` and drive the host's forward pass over the socket.
    On success ``END`` carries the ``torch.save`` blob of the block's
    ``nnsight.save()``-marked values (ready for the host to upload as-is, no pickle
    round-trip) plus ``deserialize_ms`` — deserialize happens here, so the host
    can't time that phase itself.

    ``dtype`` is the model's, shipped from the host because this process has no
    model to ask. It matters: the block runs under the same autocast bracket the
    in-process path uses, and without it an untrusted submission computed at
    different numerics from the identical trusted one.

    ``seed`` rides along for the same reason and is usually ``None``. It is set
    when several processes must draw the *same* numbers within one request — a
    tensor-parallel group, where the block's RNG now lives out here rather than
    in the rank. Seeding the rank would seed the wrong process.

    ``env`` is the request's per-request environment, the same dict the host
    applied to the loaded model just before this — see :func:`set_env` for why
    the runner needs it too.
    """
    global link
    link = Link(conn)

    # Deserialization is separated from execution because the two fail for
    # entirely different reasons and only the second has a user traceback worth
    # sending. Sharing one `except` meant a corrupt payload came back dressed as
    # a user-code failure: NDIF's own frames, this file's path, and a
    # `ZstdError` the caller could do nothing with.
    # The same per-request source-cache hygiene the model actor applies. A fresh
    # runner per request makes it redundant today; it is here so that stops being
    # load-bearing, because the failure it prevents is a block silently running
    # the *previous* request's code (see nnsight's `block_scope`).
    with block_scope():
        _run(conn, blob, compress, dtype, seed, env)


def _run(
    conn,
    blob: bytes,
    compress: bool,
    dtype: str,
    seed: "int | None",
    env: "dict | None" = None,
) -> None:
    try:
        # Before deserializing, not after: the map the unpickler resolves against
        # has to describe the tree this request asked for.
        set_env(env)
        # Same rebuild as a normal server (code recompiled, scope restored), but
        # with our unpickler so persistent ids resolve to the IPC interleaver /
        # this runner's meta model.
        deserialize_started = time.time()
        tracer = BackendRequestModel.deserialize(
            blob,
            persistent_objects=PERSISTENT_OBJECTS,
            compress=compress,
            unpickler=IPCCloudUnpickler,
        )
        deserialize_ms = round((time.time() - deserialize_started) * 1000, 2)
    except RequestError as error:
        # No user frames exist yet, so there is no traceback worth sending —
        # just the sentence BackendRequestModel.deserialize already classified
        # this into. The in-process path raises the very same object; only text
        # crosses this socket, so the runner ships str() of it, plus the type's
        # name so telemetry records what failed rather than the wrapper (#280).
        terminal = Failed(str(error), type(error).__name__)
    else:
        try:
            # The shared executor, not a copy of it: the autocast bracket and the
            # save collection have to be identical to the in-process path, or the
            # same block returns different numbers depending on whether the
            # request was trusted.
            saved = execute_traced_block(tracer, resolve_dtype(dtype), seed)
            buffer = io.BytesIO()
            torch.save(saved, buffer, pickle_module=cpu_pickle_module())
        except Exception as error:
            # Format the traceback here, where it's live: an exception loses its
            # __traceback__ when cloudpickled across the socket. A worker error already
            # carries a clean, intervention-only traceback (stashed by Mediator.switch);
            # otherwise drop nnsight's plumbing. Send the formatted text, not the object.
            import traceback

            from nnsight.tracing.util import clean_traceback

            tb = getattr(error, "__intervention_tb__", None) or clean_traceback(
                error.__traceback__
            )
            message = "".join(traceback.format_exception(type(error), error, tb))
            # The type's *name* rides along: the host wraps the text in a
            # RunnerError, and without the name a user mistake is recorded in
            # telemetry as the wrapper — OutOfOrderError trusted, RunnerError
            # untrusted, for the identical block (#280).
            terminal = Failed(message, type(error).__name__)
        else:
            terminal = End(buffer.getvalue(), deserialize_ms)
    # Drain a trailing partial print line (stdout is the runner's line-buffering
    # Writer here) so it lands before the terminal event on the socket.
    sys.stdout.flush()
    link.send(terminal)
