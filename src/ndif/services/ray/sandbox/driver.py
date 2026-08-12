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
from nnsight.intervention.interleaver import EarlyStopException, Mediator
from nnsight.util import apply

from ..deployments.modeling.nns import request_dtype

class RunnerError(Exception):
    """A failure raised by the user's block in the runner, carrying its already-
    formatted traceback (tracebacks don't survive cloudpickle, so the runner
    formats the text and ships that; see ``nns.run``)."""


class ShippingCache(Cache):
    """A ``tracer.cache()`` observed on the host on the runner's behalf.

    Reuses :class:`~nnsight.intervention.cache.Cache`'s filtering and transform (via
    ``observe`` / ``_record``), but ships each kept value to the runner as a
    ``CACHE_HIT`` instead of storing it — the runner's real Cache does the storing.
    Attached to a proxy's ``caches`` so ``Interleaver.handle`` feeds it every
    location the forward reaches, narrowed to that worker's rows.
    """

    def __init__(self, cache_id, connection, config) -> None:
        targets = config["targets"]
        super().__init__(
            None,
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
        """Store the worker's latest park, tagging its raw location with the
        occurrence this proxy's counter (or the pin) resolves it to — so the
        inherited ``handle`` sees the same ``(event, "{loc}.i{n}", *rest)`` shape
        it would from a local greenlet. ``None`` means the worker finished."""
        if park is None:
            self.pending = None
            return
        # TODO: tracer.barrier() parks a 2-tuple (no pin), which the unpack
        # below can't destructure — it raises here. The barrier primitive is
        # unsupported on the sandbox path for now.
        event, location, pin, *rest = park
        if event in ("SOURCE", "CALL", "CACHE"):
            # A control event, not a model location: SOURCE (instrument a module),
            # CALL (run a module's forward ad hoc), or CACHE (observe for a
            # tracer.cache()). Left untagged for settle_control, payload in `rest`.
            self.pending = (event, location, *rest)
            return
        self.iteration = pin
        occurrence = pin if pin is not None else self.iterations[location]
        self.pending = (event, f"{location}.i{occurrence}", *rest)

    def settle_control(self) -> None:
        # Drain control parks that name no forward location: SOURCE (source-instrument
        # a module so its ops fire), CALL (run a module's forward ad hoc), and CACHE
        # (attach a shipping cache so this worker's tracer.cache() fills over IPC).
        # Reply to the worker and resume it — repeat until it parks on a real
        # location, so `handle` only ever sees model locations. Runs before the
        # forward and after every switch.
        while self.pending is not None and self.pending[0] in ("SOURCE", "CALL", "CACHE"):
            kind = self.pending[0]
            if kind == "SOURCE":
                reply = self.driver.install_source(self.pending[1])
            elif kind == "CALL":
                _, path, hook, call_args, call_kwargs = self.pending
                reply = self.driver.run_module(path, hook, call_args, call_kwargs)
            else:  # CACHE: observe on the runner's behalf, shipping hits back to it
                _, cache_id, config = self.pending
                self.caches.append(ShippingCache(cache_id, self.connection, config))
                reply = None
            self.connection.send(("RESUME", self.id, (reply,), self.iteration))
            event, rest, _ = self.driver.next_event(self.connection)
            if event == "STOP":
                raise EarlyStopException()
            self.adopt(rest[1])

    def start(self, interleaver=None) -> None:
        # Interleaver.__enter__ starts every mediator; this proxy's worker lives in
        # the runner and its first park already arrived in the INTERLEAVE message,
        # so there's no greenlet to spin up here — just record the run it belongs to
        # (handle() reads batch scoping off it).
        self.interleaver = interleaver

    @property
    def alive(self) -> bool:
        # A worker with a pending park is still mid-intervention; None means done.
        return self.pending is not None

    def switch(self, *args):
        # Resume this worker in the runner (args carry a read's value, already
        # narrowed to this worker's rows, or nothing for a swap) and return its next
        # park. Push our pin so the runner relaxes tracer.iter in lockstep.
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

    def pump(self, connection) -> "tuple[bytes, Optional[float]]":
        """Service the runner until it reports the block finished.

        Returns ``(saved-values blob, deserialize_ms)``. The blob is already a
        ``torch.save`` of what the block kept, ready to upload as-is.

        Raises:
            RunnerError: the block failed, carrying the traceback the runner
                formatted (tracebacks don't survive cloudpickle).
        """
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
        """
        while True:
            values, kwargs = connection.recv()
            event, *rest = values
            if event == "PRINT":
                self._on_log(rest[0] if rest else "")
                continue
            if event == "EXCEPTION":
                raise RunnerError(rest[0] if rest else "sandbox error")
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
        return its operation names — the runner's IPCSource asks for this over a
        SOURCE event because it holds no modules. ``None`` when the ``forward``
        can't be sourced, so the runner reports it like the local path would."""
        from nnsight.intervention.source import SourceNotAvailable, install_source

        try:
            return list(install_source(self._envoy_at(path)).names)
        except SourceNotAvailable:
            return None

    def run_module(self, path: str, hook: bool, args, kwargs):
        """Run the module at ``path``'s forward on ``args`` and return its output —
        the host side of the runner's ``IPCEnvoy.__call__`` (an ad-hoc module call,
        e.g. the logit lens, where the module lives here). Mirrors ``Envoy.__call__``:
        ``hook=False`` calls ``forward`` directly (no hooks, its real place in the
        pass untouched); ``hook=True`` runs the full module so its hooks fire."""
        envoy = self._envoy_at(path)
        args, kwargs = self._to_device((args, kwargs))
        if hook:
            return envoy(*args, **kwargs)
        return envoy._module.forward(*args, **kwargs)

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
        connection.send(("DONE", result))

    def check_dangling(self, connection, proxies) -> None:
        """Surface workers still parked after the run.

        A proxy whose worker never got the location it wanted asks the runner to
        throw OutOfOrderError into that worker (the runner warns instead for an
        open-ended ``tracer.iter`` that outran the model). Mirrors the parent-side
        ``Interleaver.check_dangling_mediators``, now that the parent lives here.
        """
        for proxy in proxies:
            if not proxy.alive:
                continue
            requester = proxy.pending[1]
            connection.send(("THROW", proxy.id, requester, proxy.iteration != 0))
