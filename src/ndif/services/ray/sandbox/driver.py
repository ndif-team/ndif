"""Driving a model on behalf of a runner process.

The **host half** of a sandboxed request: the runner holds the user's block and
its workers, this holds the model, and the two take turns over a socket. As the
forward pass reaches each location, the matching worker is resumed in the runner,
handed the value, and its edit written back.

Deliberately free of Ray, of the request types, and of the actor:
a driver needs a model, a dtype, a socket and somewhere to put log lines. That is
what lets something other than the model actor be a host — a tensor-parallel
shard has a model and a socket and is not an actor, and a test has neither a Ray
cluster nor a queue. `SandboxModelDeployment` is now a thin adapter that owns the
runner pool and hands the driver a connection.

The split follows the wire. Everything here answers the runner; everything in the
actor answers the client.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import torch

from nnsight.intervention.batching import Batcher
from nnsight.intervention.cache import Cache
from nnsight.intervention.interleaver import (
    EarlyStopException,
    Event,
    Mediator,
    Pending,
)
from nnsight.util import apply

from ..deployments.modeling.nns import request_dtype
from .protocol import KEEP_PIN

class RunnerError(Exception):
    """A failure raised by the user's block in the runner, carrying its already-
    formatted traceback (tracebacks don't survive cloudpickle, so the runner
    formats the text and ships that; see ``nns.run``).

    ``cause_type`` is the terminal exception's class name, shipped beside the
    text because the type survives the socket no better than the traceback
    does. It exists for telemetry: the wrapper's own name says "sandbox", not
    what failed, so recording it attributed a user mistake to infrastructure —
    the same block's error counted as ``OutOfOrderError`` trusted and as this
    class (or worse, a pipe error) untrusted (#280). ``error_name``
    (``model.py``) reads it so both paths record the real type.
    """

    def __init__(
        self, message: str = "sandbox error", cause_type: "Optional[str]" = None
    ) -> None:
        super().__init__(message)
        self.cause_type = cause_type


class ShippingCache(Cache):
    """A ``tracer.cache()`` observed on the host on the runner's behalf.

    Reuses :class:`~nnsight.intervention.cache.Cache`'s filtering and transform (via
    ``observe`` / ``_record``), but ships each kept value to the runner as a
    ``CACHE_HIT`` instead of storing it — the runner's real Cache does the storing.
    Attached to a proxy's ``caches`` so ``Interleaver.handle`` feeds it every
    location the forward reaches, narrowed to that worker's rows.
    """

    def __init__(self, cache_id, connection, config, model=None) -> None:
        targets = config["targets"]
        super().__init__(
            # The host's model, not None. `Cache.subscriptions` reads every
            # module's path off it when the cache names no explicit targets —
            # which is what a bare `tracer.cache()` asks for — so without it the
            # run dies in `Interleaver.__enter__` with 'NoneType' object has no
            # attribute 'modules'. The runner cannot supply one: its copy is on
            # meta and the forward runs here.
            model,
            modules=list(targets) if targets is not None else None,
            device=config["device"],
            dtype=config["dtype"],
            detach=config["detach"],
            include_output=config["include_output"],
            include_inputs=config["include_inputs"],
        )
        self._cache_id = cache_id
        self._connection = connection

    def _record(self, path, key, value):
        self._connection.send(("CACHE_HIT", self._cache_id, path, key, value))


class MediatorProxy(Mediator):
    """The host-side parent half of one runner worker.

    The worker greenlet lives in the runner; this proxy owns the model-facing
    parent logic — the occurrence counter, the ``tracer.iter`` pin, the read/swap
    matching, and the ``batch_group`` scoping, all inherited from :class:`Mediator`
    — and drives its worker over the socket instead of by greenlet switch. One proxy
    per worker (by id) is what lets iteration state and batch rows be tracked per
    mediator.

    The base ``handle`` runs unchanged: it expects a tagged ``pending``, so each
    untagged park the worker sends is re-tagged in :meth:`adopt`; ``switch`` is
    redirected from a greenlet hop to a socket round-trip; and control parks
    (SOURCE/CALL) are drained in :meth:`settle_control` before ``handle`` sees them.
    """

    def __init__(self, mediator_id, connection, driver, park) -> None:
        # A proxy never runs block code (it drives its runner worker over the
        # socket), so the Mediator's code/globals/locals are unused dummies.
        super().__init__(None, {}, {})
        self.id = mediator_id
        self.connection = connection
        self.driver = driver
        self.adopt(park)

    def adopt(self, park) -> None:
        """Store the worker's latest park, resolving the occurrence its raw
        location is waiting for — so the inherited ``handle`` sees the same
        ``Pending`` shape it would from a local greenlet. ``None`` means the
        worker finished."""
        if park is None:
            self.pending = None
            return
        event, location, pin, *rest = park
        if event is Event.BARRIER:
            # A barrier arrival. The runner's patched Barrier.__call__ parks
            # every arrival here — `location` identifies the barrier, `value`
            # is the count it was built with — and the host does the counting
            # (`SandboxDriver.barrier_arrival`), matching where iteration
            # authority already lives. Like a control park it carries no pin
            # (the slot is None), so `self.iteration` is left alone; the
            # release RESUME sends KEEP_PIN for the same reason. The iteration
            # stays None so the park can never match a model visit.
            self.pending = Pending(event, location, None, *rest)
            self.driver.barrier_arrival(self)
            return
        if event in ("SOURCE", "CALL", "CACHE"):
            # A control event, not a model location: SOURCE (instrument a module),
            # CALL (run a module's forward ad hoc), or CACHE (observe for a
            # tracer.cache()). Its payload rides in `value` as one tuple, since a
            # Pending has no room for several items; settle_control unpacks it.
            # The iteration stays None so it is never mistaken for a location
            # this run can reach.
            self.pending = Pending(event, location, None, *rest)
            return
        self.iteration = pin
        # The occurrence a relaxed worker wants is the next visit the model has
        # not handled yet, which the base class reads off the interleaver's
        # counts (`Mediator.occurrence`) rather than a counter of its own.
        occurrence = pin if pin is not None else self.occurrence(location)
        self.pending = Pending(event, location, occurrence, *rest)

    def settle_control(self) -> None:
        # Drain control parks that name no forward location: SOURCE (source-instrument
        # a module so its ops fire), CALL (run a module's forward ad hoc), and CACHE
        # (attach a shipping cache so this worker's tracer.cache() fills over IPC).
        # Reply to the worker and resume it — repeat until it parks on a real
        # location, so `handle` only ever sees model locations. Runs before the
        # forward and after every switch.
        while self.pending is not None and self.pending.event in ("SOURCE", "CALL", "CACHE"):
            kind = self.pending.event
            if kind == "SOURCE":
                reply = self.driver.install_source(self.pending.provider)
            elif kind == "CALL":
                hook, call_args, call_kwargs = self.pending.value
                reply = self.driver.run_module(
                    self.pending.provider, hook, call_args, call_kwargs
                )
            else:  # CACHE: observe on the runner's behalf, shipping hits back to it
                (config,) = self.pending.value
                self.caches.append(
                    ShippingCache(
                        self.pending.provider,
                        self.connection,
                        config,
                        self.driver.model,
                    )
                )
                reply = None
            # KEEP_PIN, not self.iteration: a control park carried no pin (adopt
            # left `self.iteration` at whatever the last *model* park said), and
            # the worker may have advanced its own pin since then — a
            # `tracer.iter` loop moves it between parks, and `.source` parks a
            # SOURCE on every access, so a loop body that touches `.source`
            # parks one right after the pin advanced. Pushing the stale copy
            # wound the worker back to the previous step, and its next read
            # asked for an occurrence the model was already past (#296).
            self.connection.send(("RESUME", self.id, (reply,), KEEP_PIN))
            event, rest, _ = self.driver.next_event(self.connection)
            if event == "STOP":
                raise EarlyStopException()
            self.adopt(rest[1])

    def release(self) -> None:
        # Resume this worker out of a barrier park and adopt wherever it parks
        # next. Empty args — a barrier call returns nothing — and KEEP_PIN,
        # because the barrier park carried no pin, so the host's copy is stale
        # for the same reason a control park's is (#296); the worker re-ships
        # its own pin on its next model park. The new park may itself be a
        # control park (drained here) or another barrier arrival (adopt
        # re-registers it, so chained or reused barriers round correctly).
        self.connection.send(("RESUME", self.id, (), KEEP_PIN))
        event, rest, _ = self.driver.next_event(self.connection)
        if event == "STOP":
            raise EarlyStopException()
        self.adopt(rest[1])
        self.settle_control()

    def start(self, interleaver=None) -> None:
        # Interleaver.__enter__ starts every mediator; this proxy's worker lives in
        # the runner and its first park already arrived in the INTERLEAVE message,
        # so there's no greenlet to spin up here — just record the run it belongs to
        # (handle() reads batch scoping off it) and the counts it started at, which
        # is what `occurrence` measures this worker's visits against.
        self.interleaver = interleaver
        self.counts_at_start = (
            dict(interleaver.counts) if interleaver is not None else {}
        )

    @property
    def alive(self) -> bool:
        # A worker with a pending park is still mid-intervention; None means done.
        return self.pending is not None

    def switch(self, *args):
        # Resume this worker in the runner (args carry a read's value, already
        # narrowed to this worker's rows, or nothing for a swap) and return its next
        # park. Push our pin so the runner relaxes tracer.iter in lockstep.
        answered = self.pending
        if (
            args
            and answered is not None
            and answered.event is Event.VALUE
            and answered.provider.endswith(".fn")
        ):
            # Serving a `{path}.fn` read: the live callable a worker asked to
            # drill into (recursive `.source`) is in hand exactly once, here.
            # Build the host's instrumented copy now, so when this handle
            # returns, `run_op` finds it and runs it in place of the original.
            self.driver.build_recursive_source(answered.provider[: -len(".fn")], args[0])
        self.connection.send(("RESUME", self.id, args, self.iteration))
        event, rest, _ = self.driver.next_event(self.connection)
        if event == "STOP":
            # A worker asked to halt the run; unwind the forward pass, which the
            # model's interleaver __exit__ swallows as an intentional early stop.
            raise EarlyStopException()
        self.adopt(rest[1])
        self.settle_control()
        return self.pending



class SandboxDriver:
    """Runs a model for a runner over one connection.

    Args:
        model: the loaded nnsight model this driver runs.
        dtype: the model's dtype, for the autocast region a request runs in.
        on_log: where a runner's ``PRINT`` goes. The actor forwards it to the
            client as a LOG; anything without a client drops it.
    """

    def __init__(
        self,
        model: Any,
        dtype: Any,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.model = model
        self.dtype = dtype
        self._on_log = on_log if on_log is not None else (lambda text: None)
        # Barrier id -> the proxies parked on it this round (see barrier_arrival).
        # Reset per interleave: a barrier is scoped to one trace, and the id is a
        # runner-process address a later trace's barrier could legitimately reuse.
        self._barriers: "dict[str, list[MediatorProxy]]" = {}
        # Op paths a worker asked to drill into this trace (recursive `.source`).
        # `install_source` arms them on the model interleaver's `sourced`, but a
        # request that arrives with the initial parks is armed *before* the run
        # is entered and `Interleaver.__enter__` clears `sourced` — so they are
        # kept here too and re-armed just after entry (`interleave`). Reset per
        # interleave, like `sourced` itself (per-run, cleared on entry).
        self._armed: "set[str]" = set()

    def pump(self, connection) -> "tuple[bytes, Optional[float]]":
        """Service the runner until it reports the block finished.

        Returns ``(saved-values blob, deserialize_ms)``. The blob is already a
        ``torch.save`` of what the block kept, ready to upload as-is.

        Raises:
            RunnerError: the block failed, carrying the traceback the runner
                formatted (tracebacks don't survive serialization).
        """
        # Set once, here, because this is where a host takes charge of a runner:
        # every tensor the runner sends is then rebuilt straight onto this host's
        # card. It matters because the processes on this socket do not share a
        # device -- one runner serves a whole tensor-parallel group, so a tensor
        # rebuilt from its bytes would otherwise arrive on the sender's card and be
        # mixed into a different rank's forward.
        connection.map_location = self.model.device
        while True:
            name, rest, kwargs = self.next_event(connection)
            if name == "INTERLEAVE":
                fn_name, parks, *args = rest
                self.interleave(connection, fn_name, parks, args, kwargs)
            elif name == "END":
                data = rest[0] if rest else b""
                deserialize_ms = rest[1] if len(rest) > 1 else None
                return data, deserialize_ms

    def next_event(self, connection):
        """Next event from the process, servicing PRINT and raising EXCEPTION.

        Returns ``(event, rest, kwargs)`` for the first event that isn't a PRINT
        (echoed as a LOG) — so callers waiting on a specific reply don't have to
        untangle stdout the user code emitted mid-run. An EXCEPTION carries the
        runner's already-formatted traceback text.

        Tensors arrive already on this host's device: `pump` sets the connection's
        ``map_location``, so they are rebuilt there rather than relocated after the
        fact. That is worth doing in the deserializer instead of here — a walk over
        the message afterwards allocates on the sender's card first, which under
        tensor parallelism is another rank's memory budget.
        """
        while True:
            values, kwargs = connection.recv()
            event, *rest = values
            if event == "PRINT":
                self._on_log(rest[0] if rest else "")
                continue
            if event == "EXCEPTION":
                raise RunnerError(
                    rest[0] if rest else "sandbox error",
                    rest[1] if len(rest) > 1 else None,
                )
            return event, rest, kwargs

    # -- envoy/device helpers ------------------------------------------------

    def _envoy_at(self, path: str):
        """The envoy at a dotted ``path`` (``model.transformer.h.0.mlp``); numeric
        parts index a ModuleList child."""
        envoy = self.model
        for part in path.split(".")[1:]:  # drop the leading root ("model")
            envoy = envoy[int(part)] if part.isdigit() else getattr(envoy, part)
        return envoy

    def _to_device(self, data):
        """Move every tensor in ``data`` onto the model's device (no-op off-GPU)."""
        device = self.model.device
        if device is None:
            return data
        return apply(data, lambda tensor: tensor.to(device), torch.Tensor)

    # -- control events (see MediatorProxy.settle_control) -------------------

    def install_source(self, path: str):
        """Source-instrument the module at ``path`` (permanent, idempotent) and
        describe it for the runner — the runner's IPCSource asks for this over a
        SOURCE event because instrumenting its own copy would instrument nothing:
        the forward runs here. ``None`` when the ``forward`` can't be sourced, so
        the runner reports it like the local path would.

        Returns the operation names plus the forward's source text and the line
        each operation sits on. A local ``Source`` reads those off its ``Compiled``
        to build a ``SourceEnvoy``; the runner has no ``Compiled`` of the forward
        that actually runs, so they travel with the names. Everything else on
        ``Compiled`` stays here — ``code`` is a code object and does not pickle.

        ``path`` can also be a **nested op path** (``...attn.source.
        attention_interface_1``): recursive ``.source``, where the drilled-into
        callable is a live value the forward resolves at run time, so there is
        nothing to describe yet. Arming ``interleaver.sourced[path] = None`` is
        what makes ``run_op`` serve that callable at ``{path}.fn`` when the op
        fires — to the runner's parked worker, and to
        :meth:`SandboxDriver.build_recursive_source`, which instruments it then
        (the base ``SourceEnvoy.source`` marks the same ``None``; see
        nnsight ``intervention/source.py``). The reply is ``None``: the runner
        builds its own ``Compiled`` from the callable it is served, so nothing
        needs describing — and ``_envoy_at`` could not walk a ``.source``
        segment anyway. The enclosing module's source is (re)installed first so
        the op fires at all; installing it is idempotent, and it cannot fail
        here — reaching a ``SourceEnvoy`` already installed it.
        """
        from nnsight.intervention.source import SourceNotAvailable, install_source

        module_path, _, _ = path.partition(".source.")
        try:
            compiled = install_source(self._envoy_at(module_path))
        except SourceNotAvailable:
            return None
        if module_path != path:
            self.model.interleaver.sourced.setdefault(path, None)
            self._armed.add(path)
            return None
        return {
            "names": list(compiled.names),
            "lines": dict(compiled.lines),
            "source": compiled.source,
        }

    def build_recursive_source(self, path: str, fn) -> None:
        """Instrument the live callable at an armed op ``path``, for this forward
        to run in place of the original (the host half of recursive ``.source``).

        Called from :meth:`MediatorProxy.switch` at the one moment the callable
        exists on this side: ``run_op`` reached the drilled-into op and is
        serving it over ``{path}.fn`` to the runner's parked worker. Storing the
        instrumented copy into ``interleaver.sourced[path]`` before that handle
        returns is what makes ``run_op`` pick it up — its inner operations then
        fire under ``{path}.source.*``, ordinary locations the proxies serve.
        Mirrors the build at the bottom of the base ``SourceEnvoy.source``
        (nnsight ``intervention/source.py``), which on the trusted path runs in
        the parked worker itself.

        The error cases build nothing, deliberately: a submodule target, an
        assignment, or a callable with no Python source each make the *runner's*
        copy of this raise the trusted path's ``SourceNotAvailable`` inside the
        user's own frame (``ipc_recursive_source``, nns.py). The entry staying
        ``None`` just means the forward runs the original callable while that
        error ends the run.
        """
        from nnsight.intervention.source import (
            SourceNotAvailable,
            bind,
            instrument,
            make_op,
        )

        interleaver = self.model.interleaver
        if path not in interleaver.sourced or interleaver.sourced[path] is not None:
            return  # not an armed recursive-source request, or already built
        if isinstance(fn, torch.nn.Module) or fn is bind:
            return
        try:
            interleaver.sourced[path] = instrument(
                fn,
                make_op(
                    lambda: (interleaver, path)
                    if interleaver.interleaving
                    else (None, None)
                ),
            )
        except SourceNotAvailable:
            pass

    def run_module(self, path: str, hook: bool, args, kwargs):
        """Run the module at ``path`` ad hoc and return its output — the host side
        of the runner's ``IPCEnvoy.__call__`` (an ad-hoc module call, e.g. the
        logit lens, where the module lives here).

        Defers to ``Envoy.__call__`` instead of picking a callable itself,
        because the base call owns the occurrence semantics: ``hook=False`` runs
        the module the ordinary way with the trace stood down for the duration,
        so nothing is served and no occurrence is spent for the module *or
        anything under it* — a hand-rolled ``_module.forward(...)`` with
        ``interleaving`` still on let the controllers count the ad-hoc visit and
        steal the real occurrence, so a later read of the module's real
        ``.output`` raised ``OutOfOrderError`` (#295) — and the module's own
        runtime wrappers still fire (transformers tensor parallelism keeps its
        collectives there; a bare ``forward`` returns one rank's slice).
        ``hook=True`` lets the trace watch the call, for a module attached to
        the tree rather than one the forward already runs. The host's ``Envoy``
        is the real, unpatched class (the IPC patches load only in the runner),
        so this is the trusted path's behavior by construction."""
        return self._envoy_at(path)(*args, hook=hook, **kwargs)

    def barrier_arrival(self, proxy: "MediatorProxy") -> None:
        """One worker arrived at a barrier; release the round when it is full.

        The host-authority half of ``tracer.barrier()``: the runner's patched
        ``Barrier.__call__`` parks every arrival over the socket instead of
        counting in-process (where a release moved workers the host never heard
        about and their saves were silently dropped, #294), so the counting
        lives here, beside the occurrence counter and the pin.

        ``proxy.pending`` is the arrival: ``provider`` identifies the barrier
        and ``value`` is the ``n`` it was built with. Arrivals accumulate under
        the barrier's id; the nth releases the whole round **sequentially** —
        earlier arrivals first, the completing one last, mirroring base
        ``Barrier.__call__`` — each a RESUME→PARK round trip that adopts the
        worker's next park. The registry entry is cleared *before* the resumes,
        as the base empties ``_waiting``, so a released worker that reaches the
        same barrier again starts a fresh round (adopt re-enters here).

        Works the same wherever the arrival lands: an initial park at
        INTERLEAVE time (every block reaches the barrier before touching the
        model — the round completes in ``_build_proxies``, before the forward)
        or mid-forward (a released worker's new park joins the visit the host
        is still serving, exactly as base ``Interleaver.handle`` promises).
        """
        pending = proxy.pending
        waiting = self._barriers.setdefault(pending.provider, [])
        waiting.append(proxy)
        if len(waiting) < pending.value:
            return
        del self._barriers[pending.provider]
        for waiter in waiting:
            waiter.release()

    # -- the interleaved run --------------------------------------------------

    def _build_proxies(self, connection, parks, batch_groups):
        """One :class:`MediatorProxy` per runner worker, each scoped to its batch
        rows, with any initial SOURCE/CALL control park resolved before the forward
        runs (so ``handle`` only ever sees model locations)."""
        proxies = [
            MediatorProxy(mediator_id, connection, self, park)
            for mediator_id, park in enumerate(parks)
        ]
        for proxy, group in zip(proxies, batch_groups):
            proxy.batch_group = group  # narrow each read to this invoke's rows
        for proxy in proxies:
            proxy.settle_control()
        return proxies

    def _assemble(self, fn, invokes, kwargs):
        """The batcher and the on-device ``(args, kwargs)`` for one combined call.

        The pipeline / tokenizer that turn text into model inputs live here on the
        host (not in the runner), so assembly happens here — mirroring
        ``Envoy.interleave``. Trace-level kwargs (e.g. ``max_new_tokens``) win over
        the assembled ones. The batcher's ``total`` + the proxies' batch groups are
        what let ``handle`` narrow/widen per invoke."""
        batcher = Batcher(self.model)
        for inputs, invoke_kwargs in invokes:
            batcher.add(*inputs, **invoke_kwargs)
        args, assembled = batcher.assemble(fn)
        # Still needed with `map_location` doing the rest: the batcher and tokenizer
        # run *here*, so these tensors are made on this host and never crossed the
        # wire. Nothing else would put them on the model's card.
        args, kwargs = self._to_device((args, {**assembled, **kwargs}))
        return batcher, args, kwargs

    def interleave(self, connection, fn_name, parks, args, kwargs) -> None:
        """Run ``fn_name`` on the host, interleaved with the process's workers.

        One ``MediatorProxy`` per worker drives it over the socket as the forward
        pass reaches its locations; reads/swaps flow through exactly as nnsight's
        local mediators would. After the run, dangling workers are surfaced and the
        model's result is shipped to the process to return to the client.
        """
        interleaver = self.model.interleaver
        # The runner packs each worker's batch group (row range) and the raw
        # per-invoke inputs positionally after `parks`.
        batch_groups = args[0] if args else []
        invokes = args[1] if len(args) > 1 else []
        # Fresh barrier rounds per trace: a barrier's id is a runner-process
        # address, which a later trace in the same session could reuse. The
        # armed recursive-source paths are per-trace for the same reason.
        self._barriers = {}
        self._armed = set()
        proxies = self._build_proxies(connection, parks, batch_groups)
        interleaver.mediators = proxies
        result = None
        try:
            # The same autocast region the in-process path runs a request in.
            # It has to be *here*: the runner brackets its own work, but the
            # forward happens in this process, so without this the model's own
            # arithmetic ran outside autocast and an untrusted request came back
            # with different numbers than the identical trusted one. Measured on
            # gpt2: identical token ids and embeddings, diverging inside the
            # first block.
            with request_dtype(self.dtype), interleaver:
                # Re-arm recursive-source requests that arrived with the initial
                # parks: `install_source` armed them in `_build_proxies`, before
                # this entry, and `Interleaver.__enter__` clears `sourced`.
                # A mid-run arm (settle_control after a switch) lands after the
                # clear and stays put on its own.
                for armed in self._armed:
                    interleaver.sourced.setdefault(armed, None)
                fn = getattr(self.model, fn_name)
                interleaver.batcher, call_args, call_kwargs = self._assemble(
                    fn, invokes, kwargs
                )
                result = fn(*call_args, **call_kwargs)
                # Serve the return value to any worker parked on `tracer.result`.
                interleaver.handle("result", result)
            self.check_dangling(connection, proxies)
        finally:
            # Leave the interleaver clean so the next run starts fresh.
            interleaver.mediators = []
            interleaver.batcher = None
        try:
            connection.send(("DONE", result))
        except OSError:
            # A fatal THROW (check_dangling) makes the runner unwind, report
            # EXCEPTION and close its socket — possibly before this send. The
            # report is already buffered for `pump` to read; raising the broken
            # send instead would mask the user's real error with a
            # BrokenPipeError (#280's shape).
            pass

    def check_dangling(self, connection, proxies) -> None:
        """Surface workers still parked after the run.

        Classifies each dangling park the way nnsight's ``dangling_unwind``
        does, event first — the THROW carries the kind and the runner builds
        the matching error:

        * ``BARRIER`` — fewer blocks reached the barrier than it was built
          for, so it was never going to release. The runner raises the base
          ValueError; before this branch existed the barrier park's ``None``
          iteration fell into the iter test below and a dropped save was
          *warned* about as a loop overrun (#294).
        * ``ITER`` — ``iteration != 0`` (a pinned step, or ``None`` after a
          pin relaxed, which only a ``tracer.iter`` produces): the loop
          outran the model; the runner unwinds and warns, keeping what was
          saved. Base reads the same from ``mediator.iteration == 0``.
        * ``OUT_OF_ORDER`` — a plain request for a location the model never
          reached; the runner raises OutOfOrderError into the worker.

        Mirrors the parent-side ``Interleaver.check_dangling_mediators``, now
        that the parent lives here.
        """
        for proxy in proxies:
            if not proxy.alive:
                continue
            if proxy.pending.event is Event.BARRIER:
                kind = "BARRIER"
            elif proxy.iteration != 0:
                kind = "ITER"
            else:
                kind = "OUT_OF_ORDER"
            try:
                connection.send(("THROW", proxy.id, str(proxy.pending), kind))
            except OSError:
                # A fatal kind already thrown makes the runner unwind, report
                # EXCEPTION and close its socket, racing the rest of this loop.
                # Its report is buffered for `pump` to read; stop sending so a
                # BrokenPipeError can't mask the user's real error (#280's
                # shape).
                return
