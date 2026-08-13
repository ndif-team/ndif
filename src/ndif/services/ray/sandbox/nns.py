"""nnsight glue for the process sandbox.

The user's traced block is deserialized and executed *inside* the runner process
(nnsight must be importable there). The model, though, lives on the host — so the
intervention code runs here while the forward pass runs there, and the two are
interleaved over the socket.

The interleaver is split across the socket at the mediator level:

* The intervention **workers** (greenlets) run in this process. A worker still
  parks on a location, but instead of resolving its ``.i{n}`` occurrence tag here
  it sends the raw location plus its current ``tracer.iter`` pin to the host
  (patched ``Mediator.event`` below).
* Each worker's **parent side** — the occurrence counter, the pin, and the
  read/swap matching — lives on the host as one proxy per worker. The host drives
  each worker individually over the socket: ``pump`` resumes a worker on RESUME
  and returns its next park, throws OutOfOrderError on THROW, and ends on DONE.

So iteration state is tracked per mediator on the host, and a worker's activation
values only cross when the host resumes it (a read) or it swaps (in its park).
Because a read hands back a *copy*, ``pump`` writes each read's value back after
the worker moves past it, so in-place edits (``output[:] = 0``) reach the host.
Two things the runner can't do with a *weightless* module — ``.source`` and an
ad-hoc module call (``IPCEnvoy.__call__``) — become SOURCE / CALL events the host
serves.

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

from nnsight.intervention.cache import Cache
from nnsight.intervention.envoy import Envoy
from nnsight.intervention.interleaver import (
    EarlyStopException,
    Event,
    Interleaver,
    Mediator,
    OutOfOrderError,
)
from nnsight.intervention.serialization import CustomCloudUnpickler
from nnsight.intervention.source import SourceEnvoy
from nnsight.intervention.tracer import InterleavingTracer
from nnsight.modeling.mixins.remotable import Remotable

from ....common.errors import RequestError
from ....common.schema.request import BackendRequestModel
from ..deployments.modeling.nns import block_scope, execute_traced_block
from ..deployments.modeling.util import cpu_pickle_module, resolve_dtype

# The runner's connection to the host, for the request currently executing. The
# runner handles one request at a time, so a module global is enough.
connection = None


def ipc_event(cls, event, location, *rest):
    """Park by handing the host the raw location + current pin, not a tagged one.

    The base ``Mediator.event`` resolves the ``.i{n}`` occurrence tag here from the
    worker's own counter. In the split the host owns that counter (one proxy per
    worker), so a park carries only ``(event, location, pin, *rest)`` and the host
    tags/matches it. ``pin`` is the worker's ``tracer.iter`` pointer (or None).
    """
    worker = getcurrent()
    return worker.parent.switch((event, location, worker.mediator().iteration, *rest))


# Every mediator in this process is a worker whose parent lives on the host, so
# park untagged. (nns is imported only in the runner, so this never hits the host.)
Mediator.event = classmethod(ipc_event)


def read_of(park):
    """The read a park represents — ``(location, occurrence)`` — or ``None`` if the
    park isn't a value read. pump tracks these to write in-place edits back."""
    if park is not None and park[0] is Event.VALUE:
        return (park[1], park[2])
    return None


class IPCInterleaver(Interleaver):
    """Interleaver whose model side is the host, reached over a socket.

    The workers (greenlets) run here; there are no modules to hook. The host owns
    the parent side and drives each worker by id: :meth:`pump` resumes a worker on
    RESUME (replying with its next park), throws into one on THROW, and returns the
    model's result on DONE.
    """

    def __init__(self, connection) -> None:
        super().__init__()
        self.connection = connection
        # tracer.cache() caches, by id — the host observes on our behalf and ships
        # each kept value back as a CACHE_HIT for us to record here.
        self.caches: dict[int, Cache] = {}

    def instrument(self, envoy: Envoy) -> None:
        # This process only has meta modules — the forward runs on the host, so
        # hooking here would fire on nothing.
        pass

    def pump(self):
        """Drive the workers from the host until the run ends.

        RESUME switches a worker in (a read's value, or nothing for a swap) and
        replies with its next park; the host pushes the worker's pin so tracer.iter
        relaxation stays in lockstep. THROW surfaces a dangling worker. DONE carries
        the model's result and ends the loop. Every RESUME also writes the previous
        read's value back (see :meth:`writeback`) so in-place edits reach the host.

        ``reads`` tracks each worker's outstanding read (by mediator id), seeded from
        the parks already shipped in the INTERLEAVE message.
        """
        reads = {
            mediator_id: read
            for mediator_id, mediator in enumerate(self.mediators)
            if (read := read_of(mediator.pending)) is not None
        }
        while True:
            message = self.connection.recv()
            kind = message[0]
            if kind == "DONE":
                return message[1]
            if kind == "RESUME":
                _, mediator_id, args, pin = message
                mediator = self.mediators[mediator_id]
                mediator.iteration = pin
                # The read this RESUME answers, if any — the worker has held its
                # (copied) value and may have edited it in place.
                read = reads.pop(mediator_id, None)
                try:
                    park = mediator.switch(*args)
                except EarlyStopException:
                    # A worker halted the run (tracer.stop()); tell the host to
                    # unwind the model, then let the stop propagate as nnsight's does.
                    self.connection.send("STOP")
                    raise
                if read is not None:
                    self.writeback(mediator_id, read, args[0])
                if (new_read := read_of(park)) is not None:
                    reads[mediator_id] = new_read
                self.connection.send("PARK", mediator_id, park)
            elif kind == "THROW":
                _, mediator_id, requester, is_iter = message
                self.throw(self.mediators[mediator_id], requester, is_iter)
            elif kind == "CACHE_HIT":
                # A location one of our caches wants was reached on the host; record
                # the (already filtered + moved-to-cpu) value into that cache.
                _, cache_id, path, key, value = message
                self.caches[cache_id]._record(path, key, value)

    def writeback(self, mediator_id, read, value):
        """Swap a read's (maybe-edited) value back at its location, then absorb the
        host's resume for the worker's next park.

        A read hands the worker a *copy* of the model's value (it crossed the
        socket), so an in-place edit — ``output[:] = 0`` — never reaches the host on
        its own. The read left the host parked at that location, so swapping now
        applies any edit to the batch before the model moves on. We always write
        back (any object may have been edited, and we can't tell from here);
        narrow/widen preserve the container type, so a read-only value is unchanged.
        """
        # TODO: only a value SWAP is sent back — eproperty write-back transforms
        # attached to the read are silently dropped on the sandbox path.
        location, iteration = read
        self.connection.send("PARK", mediator_id, (Event.SWAP, location, iteration, value))
        self.connection.recv()

    def throw(self, mediator, requester, is_iter):
        """Throw OutOfOrderError into a worker still parked after the run.

        Mirrors ``Interleaver.check_dangling_mediators``: an open-ended
        ``tracer.iter`` that outran the model is unwound and warned about (its
        reached iterations are kept); any other dangling request is a real error
        and propagates, failing the run.
        """
        error = OutOfOrderError(
            f"'{requester}' was requested but the model already ran past it"
        )
        if is_iter:
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
                # serve the run. Dangling workers are surfaced by the host (THROW).
                mediators = self.interleaver.mediators
                parks = [mediator.pending for mediator in mediators]
                # Each worker's row range in the combined batch, so the host can
                # narrow a read to just that invoke's rows (None = whole batch: an
                # edit or empty invoke).
                batch_groups = [mediator.batch_group for mediator in mediators]
                invokes = batcher.invokes if batcher is not None else []
                connection.send(
                    "INTERLEAVE", fn_name, parks, batch_groups, invokes, **kwargs
                )
                result = self.interleaver.pump()
        except EarlyStopException:
            pass
        finally:
            self.interleaver.cancel()
        return result

    def __call__(self, *args, hook=False, **kwargs):
        # An ad-hoc module call (e.g. the logit lens): the module lives on the host,
        # so send a CALL event with the inputs, run its forward there, and hand the
        # output back. `hook` mirrors Envoy.__call__ (direct forward vs full module).
        return getcurrent().parent.switch(("CALL", self.path, None, hook, args, kwargs))


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
            return IPCInterleaver(connection)
        elif pid in self.persistent_objects:
            return self.persistent_objects[pid]

        return None


class IPCSource:
    """``envoy.source`` in the runner, where the module lives on the host.

    The base :class:`~nnsight.intervention.source.Source` parses and instruments the
    module's ``forward``. The runner has a meta copy it could parse, but the forward
    that has to fire is the host's — so on construction we send a ``SOURCE`` event to
    the host, which source-instruments the real module (so its operations fire during
    the forward) and hands back the operation names for validation. Each
    ``source.<op>`` is then just a location the interleaver serves, exactly like a
    module ``.output``.
    """

    def __init__(self, envoy: Envoy) -> None:
        self._envoy = envoy
        self._prefix = f"{envoy.path}.source"
        # Round-trip to the host: install source on the module and return its op
        # names (None when the forward can't be sourced).
        self._names = getcurrent().parent.switch(("SOURCE", envoy.path, None))

    def __getattr__(self, name: str) -> SourceEnvoy:
        if name.startswith("_"):
            raise AttributeError(name)
        if self._names is not None and name not in self._names:
            available = ", ".join(self._names) or "(none)"
            raise AttributeError(
                f"{self._prefix!r} has no operation {name!r}; available: {available}"
            )
        return SourceEnvoy(self._envoy, name, f"{self._prefix}.{name}")


# Patch the whole IPCEnvoy class onto the base Envoy. The tracer's root is a model
# wrapper (TransformersModel -> ... -> Envoy) that inherits interleave from the
# base, so rebinding this module's Envoy name wouldn't reach it — the overrides
# have to land on the base class every envoy shares.
for _name, _member in vars(IPCEnvoy).items():
    if callable(_member) and _name in vars(Envoy):
        setattr(Envoy, _name, _member)

# `.source` is a property (not caught by the callable patch above): route it to the
# host-backed IPCSource so source ops work without a local module.
Envoy.source = property(lambda self: IPCSource(self))


def ipc_cache(self, *args, **kwargs):
    """``tracer.cache()`` in the runner: its values are produced on the host.

    A cache here would never fill — the forward, and so ``Interleaver.handle``, runs
    on the host. So build the ordinary Cache / CacheView (kept a plain nnsight type,
    so the client can deserialize it), then register it and send a ``CACHE`` event
    carrying the filter. The host observes on our behalf and ships each kept value
    back as a ``CACHE_HIT``, which :meth:`IPCInterleaver.pump` records into it.
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
    getcurrent().parent.switch(("CACHE", cache_id, None, config))
    return view


_original_cache = InterleavingTracer.cache
InterleavingTracer.cache = ipc_cache

# This runner's weightless model, and the persistent-id map built from it. The
# map is what `IPCCloudUnpickler` resolves a payload's ids against; the model is
# kept because it is the thing that produced the map.
META_MODEL = None
PERSISTENT_OBJECTS = {}


def load_meta_model(model_key: str, trust_remote_code: bool = False) -> None:
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

    ``trust_remote_code`` mirrors the actor's, and is the one load option that
    decides whether this build is *possible*: a checkpoint whose architecture is
    defined in its own repo has no config to read without it. It is the actor's
    setting because the tree has to match the actor's, and the repo code it runs
    has already run there.

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
    META_MODEL = Remotable.from_model_key(
        model_key, trust_remote_code=trust_remote_code
    )
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
    global connection
    connection = conn

    # Deserialization is separated from execution because the two fail for
    # entirely different reasons and only the second has a user traceback worth
    # sending. Sharing one `except` meant a corrupt payload came back dressed as
    # a user-code failure: NDIF's own frames, this file's path, and a
    # `ZstdError` the caller could do nothing with.
    # The same per-request source-cache hygiene the model actor applies. A fresh
    # runner per request makes it redundant today; it is here so that stops being
    # load-bearing, because the failure it prevents is a block silently running
    # the *previous* request's code (see nns.block_scope).
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
        # crosses this socket, so the runner ships str() of it.
        terminal = ("EXCEPTION", str(error))
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
            terminal = ("EXCEPTION", message)
        else:
            terminal = ("END", buffer.getvalue(), deserialize_ms)
    # Drain a trailing partial print line (stdout is the runner's line-buffering
    # Writer here) so it lands before the terminal event on the socket.
    sys.stdout.flush()
    conn.send(*terminal)
