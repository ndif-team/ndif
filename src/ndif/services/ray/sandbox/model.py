"""Model deployment that runs user code in a separate process (no VM).

Subclasses :class:`BaseModelDeployment`: the model still loads on the host (so
the base's caching/lifecycle are unchanged), but the request's traced block is
deserialized and executed in a pooled runner process. The intervention code runs
there while the forward pass runs here, interleaved over the socket.

When the process is ready to run the model it sends an ``INTERLEAVE`` event with
the wrapper method to call, one initial *park* per worker, each worker's batch
rows, and the raw per-invoke inputs. We build a ``MediatorProxy`` per worker,
assemble the inputs here (the pipeline lives on the host), and run the real model
under its interleaver; as the forward pass reaches each location the matching
proxies drive their workers over the socket (a read hands the value across,
narrowed to that worker's rows; a swap — including an edit written back — comes
back). Workers can also park on control events the forward doesn't serve: SOURCE
(source-instrument a module) and CALL (run a module ad hoc); ``settle_control``
resolves those. The final result goes back for the process to return to the client.
"""

import contextlib
import threading
from typing import TYPE_CHECKING, Any

import ray
import torch

from nnsight.intervention.batching import Batcher
from nnsight.intervention.cache import Cache
from nnsight.intervention.interleaver import EarlyStopException, Mediator
from nnsight.schema.response import Status
from nnsight.util import apply

from ..deployments.modeling.base import BaseModelDeployment
from .host import Pool

if TYPE_CHECKING:
    from ....common.schema.request import BackendRequestModel


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

    def __init__(self, mediator_id, connection, request, deployment, park) -> None:
        # A proxy never runs block code (it drives its runner worker over the
        # socket), so the Mediator's code/globals/locals are unused dummies.
        super().__init__(None, {}, {})
        self.id = mediator_id
        self.connection = connection
        self.request = request
        self.deployment = deployment
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
                reply = self.deployment.install_source(self.pending[1])
            elif kind == "CALL":
                _, path, hook, call_args, call_kwargs = self.pending
                reply = self.deployment.run_module(path, hook, call_args, call_kwargs)
            else:  # CACHE: observe on the runner's behalf, shipping hits back to it
                _, cache_id, config = self.pending
                self.caches.append(ShippingCache(cache_id, self.connection, config))
                reply = None
            self.connection.send(("RESUME", self.id, (reply,), self.iteration))
            event, rest, _ = self.deployment.next_event(self.connection, self.request)
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
        event, rest, _ = self.deployment.next_event(self.connection, self.request)
        if event == "STOP":
            # A worker asked to halt the run; unwind the forward pass, which the
            # model's interleaver __exit__ swallows as an intentional early stop.
            raise EarlyStopException()
        self.adopt(rest[1])
        self.settle_control()
        return self.pending


class SandboxModelDeployment(BaseModelDeployment):
    """Loads the model on the host; runs user code in a process pool."""

    def __init__(self, *args: Any, pool_size: int = 2, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pool = Pool(size=pool_size)
        # The runner currently executing (fresh per request), tracked so run() can
        # stop it to interrupt a timed-out or cancelled request.
        self.execution_sandbox = None

    def execution_scope(self, request: "BackendRequestModel"):
        # A trusted request runs in-process, so use the base's stdout capture. A
        # sandboxed one forwards stdout as PRINT events instead (see next_event),
        # so there's nothing to wrap here.
        if request.trusted:
            return super().execution_scope(request)
        return contextlib.nullcontext()

    def format_error(self, exception):
        # A user-block failure arrives as RunnerError with its traceback already
        # formatted in the runner (tracebacks don't survive cloudpickle); it's user
        # code, so never fatal. Host-side failures fall back to the base.
        if isinstance(exception, RunnerError):
            return str(exception), False
        return super().format_error(exception)

    def cleanup(self) -> None:
        # Fresh process per request: discard the runner, then do the base reset.
        self.discard_sandbox()
        super().cleanup()

    def interrupt(self) -> None:
        # Stopping the runner unblocks the host thread if it's parked on the socket;
        # the base's kill_thread covers it running the forward pass in Python.
        if self.execution_sandbox is not None:
            self.execution_sandbox.stop()
        super().interrupt()

    def discard_sandbox(self) -> None:
        """Stop the request's runner (idempotent — ``stop`` checks the process)."""
        if self.execution_sandbox is not None:
            self.execution_sandbox.stop()
            self.execution_sandbox = None

    def next_event(self, connection, request):
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
                request.respond(Status.LOG, rest[0] if rest else "")
                continue
            if event == "EXCEPTION":
                raise RunnerError(rest[0] if rest else "sandbox error")
            return event, rest, kwargs

    def execute(self, request: "BackendRequestModel") -> "tuple[bytes, float | None]":
        """Worker-thread body: run the request's block in a fresh runner.

        A trusted request skips the sandbox and runs in-process via the base.
        Otherwise acquire a fresh runner (tracked on the actor so run() can stop it
        to interrupt a timed-out/cancelled request), send the payload, and service
        INTERLEAVE events — driving the model on the host — until ``END``, whose
        ``torch.save`` blob of the saved values and the runner's deserialize time it
        returns. run() discards the runner afterward — one fresh process per request.
        """
        if request.trusted:
            return super().execute(request)
        self.execution_ident = threading.current_thread().ident
        sandbox = self.pool.acquire()
        self.execution_sandbox = sandbox
        connection = sandbox.connection()
        try:
            connection.send((request.payload, request.compress))
            while True:
                name, rest, kwargs = self.next_event(connection, request)
                if name == "INTERLEAVE":
                    fn_name, parks, *args = rest
                    self.interleave(connection, request, fn_name, parks, args, kwargs)
                elif name == "END":
                    data = rest[0] if rest else b""
                    deserialize_ms = rest[1] if len(rest) > 1 else None
                    return data, deserialize_ms
        finally:
            connection.close()

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

    def _build_proxies(self, connection, request, parks, batch_groups):
        """One :class:`MediatorProxy` per runner worker, each scoped to its batch
        rows, with any initial SOURCE/CALL control park resolved before the forward
        runs (so ``handle`` only ever sees model locations)."""
        proxies = [
            MediatorProxy(mediator_id, connection, request, self, park)
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

    def interleave(self, connection, request, fn_name, parks, args, kwargs) -> None:
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
        proxies = self._build_proxies(connection, request, parks, batch_groups)
        interleaver.mediators = proxies
        result = None
        try:
            with interleaver:
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

    def close(self) -> None:
        self.pool.close()


@ray.remote(num_cpus=1, max_restarts=-1)
class SandboxModelActor(SandboxModelDeployment):
    """Ray actor that runs user code in a separate process."""

    pass
