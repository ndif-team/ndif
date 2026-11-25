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
from nnsight.intervention.interleaver import EarlyStopException, Event, Mediator
from nnsight.util import apply

from ..deployments.modeling.nns import request_dtype
from .controls import (
    HANDLERS,
    compact,
    kept_by,
    own,
    serve_recursive_source,
    withhold,
    write_back,
)
from .link import Link
from .messages import (
    Control,
    Dangling,
    Done,
    End,
    Failed,
    Interleave,
    Park,
    Print,
    Reply,
    Resume,
    Stop,
    Throw,
)


class RunnerError(Exception):
    """A failure raised by the user's block in the runner, carrying its already-
    formatted traceback (tracebacks don't survive cloudpickle, so the runner
    formats the text and ships that; see ``execute.run``).

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


def landing(model) -> Callable:
    """``torch.load``'s ``map_location`` for what a runner sends this host.

    A tensor sent from a GPU this model's weights are on is rebuilt on that GPU.
    Anything else (a CPU tensor the block made, another rank's card) is rebuilt on
    ``model.device``.

    The first half matters for a model a device map spreads over several cards.
    A value a block edits is written back to the host, and a read of
    ``model.output`` carries the whole KV cache. Accelerate's hooks move an
    activation back to the card of the module that takes it next, but they never
    move a cache. So a cache rebuilt on ``model.device`` stays there, and the next
    forward's ``torch.cat`` in the first layer on another card fails with "Expected
    all tensors to be on the same device".

    On a host with one card, which is every tensor-parallel rank, both halves
    rebuild on ``model.device``.
    """
    cards = {str(device) for device in model.devices if device.type == "cuda"}
    home = str(model.device)

    def land(storage, location):
        if location in cards:
            return None  # torch then rebuilds it where it was saved from
        return torch.serialization.default_restore_location(storage, home)

    return land


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
    redirected from a greenlet hop to an exchange over the link (:meth:`resume`);
    and control requests are answered in :meth:`settle_control` before ``handle``
    sees them.
    """

    def __init__(self, mediator_id, link, driver, pending) -> None:
        # A proxy never runs block code (it drives its runner worker over the
        # socket), so the Mediator's code/globals/locals are unused dummies.
        super().__init__(None, {}, {})
        self.id = mediator_id
        self.link = link
        self.driver = driver
        self.adopt(pending)

    def adopt(self, pending) -> None:
        """Store the worker's latest park, resolving the occurrence its raw
        location is waiting for — so the inherited ``handle`` sees the same
        ``Pending`` shape it would from a local greenlet. ``None`` means the
        worker finished."""
        if pending is None or isinstance(pending, Control):
            # A control request names no model location and carries no pin;
            # `settle_control` answers it. Its `provider` and `iteration` are
            # None, so it is never mistaken for a location this run can reach.
            self.pending = pending
            return
        if pending.event is Event.BARRIER:
            # A barrier arrival. The runner's patched Barrier.__call__ parks
            # every arrival here — `provider` identifies the barrier, `value`
            # is the count it was built with — and the host does the counting
            # (`SandboxDriver.barrier_arrival`), matching where iteration
            # authority already lives. Like a control request it carries no pin
            # (the slot is None), so `self.iteration` is left alone; the
            # release is a `Reply` for the same reason. The iteration stays
            # None so the park can never match a model visit.
            self.pending = pending
            self.driver.barrier_arrival(self)
            return
        # A park on a model location carries the worker's pin where the
        # occurrence goes.
        if pending.event is Event.SWAP:
            # A value the block assigns. Where it holds the stand-in for a KV
            # cache, the forward gets its own cache back.
            try:
                pending = pending._replace(value=own(pending.value, self.driver.held))
            except LookupError as error:
                raise RunnerError(str(error), "ProtocolError") from error
        pin = pending.iteration
        self.iteration = pin
        # The occurrence a relaxed worker wants is the next visit the model has
        # not handled yet, which the base class reads off the interleaver's
        # counts (`Mediator.occurrence`) rather than a counter of its own.
        occurrence = pin if pin is not None else self.occurrence(pending.provider)
        self.pending = pending._replace(iteration=occurrence)

    def resume(self, message, served=None) -> None:
        """One exchange with the worker: send ``message``, which switches it in,
        and adopt the park it answers with.

        ``served`` is the value a read handed the worker. If the block edited its
        copy in place, the park brings the copy back (``Park.edited``), and it is
        written into ``served`` before anything else happens at this location.
        """
        answer = self.link.request(message, Park, Stop)
        if isinstance(answer, Stop):
            # A worker asked to halt the run; unwind the forward pass, which the
            # model's interleaver __exit__ swallows as an intentional early stop.
            raise EarlyStopException()
        if answer.edited is not None:
            write_back(served, answer.edited, self.driver.kept)
        self.adopt(answer.pending)

    def settle_control(self) -> None:
        # Answer control requests until the worker parks on a real location, so
        # `handle` only ever sees model locations. Runs before the forward and
        # after every switch.
        #
        # The answer is a `Reply`, which carries no pin: a control request sent
        # none (adopt left `self.iteration` at whatever the last *model* park
        # said), and the worker may have advanced its own pin since then — a
        # `tracer.iter` loop moves it between parks, and `.source` parks a
        # request on every access, so a loop body that touches `.source` parks
        # one right after the pin advanced. Pushing the stale copy wound the
        # worker back to the previous step, and its next read asked for an
        # occurrence the model was already past (#296).
        while isinstance(self.pending, Control):
            reply = HANDLERS[type(self.pending)](self.driver, self, self.pending)
            self.resume(Reply(self.id, (reply,)))

    def release(self) -> None:
        # Resume this worker out of a barrier park and adopt wherever it parks
        # next. Empty args — a barrier call returns nothing — and a `Reply`,
        # because the barrier park carried no pin, so the host's copy is stale
        # for the same reason it is after a control request (#296); the worker
        # re-ships its own pin on its next model park. The new park may itself
        # be a control request (answered here) or another barrier arrival (adopt
        # re-registers it, so chained or reused barriers round correctly).
        self.resume(Reply(self.id))
        self.settle_control()

    def start(self, interleaver=None) -> None:
        # Interleaver.__enter__ starts every mediator; this proxy's worker lives in
        # the runner and its first park already arrived in the Interleave message,
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
            # Describe, don't ship (#312): `serve_recursive_source` classifies
            # it, builds the instrumented copy `run_op` will find when this
            # handle returns, and hands back the description (or typed refusal)
            # that crosses the socket in the callable's place.
            args = (
                serve_recursive_source(
                    self.driver.model.interleaver,
                    answered.provider[: -len(".fn")],
                    args[0],
                ),
            )
        # What crosses is a compact copy of a read, with a stand-in for each KV
        # cache; an edit of it comes back to the value itself, which is a view
        # into the forward's own tensors.
        served = args[0] if args else None
        if args:
            args = (compact(withhold(served, self.driver.held)),)
        self.resume(Resume(self.id, args, self.iteration), served)
        self.settle_control()
        return self.pending


class SandboxDriver:
    """Runs a model for a runner over one connection.

    Args:
        model: the loaded nnsight model this driver runs.
        dtype: the model's dtype, for the autocast region a request runs in.
        on_log: where a runner's ``Print`` goes. The actor forwards it to the
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
        # `controls.source` arms them on the model interleaver's `sourced`, but a
        # request that arrives with the initial parks is armed *before* the run
        # is entered and `Interleaver.__enter__` clears `sourced` — so they are
        # kept here too and re-armed just after entry (`interleave`). Reset per
        # interleave, like `sourced` itself (per-run, cleared on entry).
        self.armed: "set[str]" = set()
        # Where the model's parameters and buffers are stored, which a block's
        # in-place edit is never written into (`controls.write_back`). Collected
        # per driver, so per request, because the addresses change when a cached
        # model is moved back onto its cards.
        self.kept = kept_by(model._module)
        # The KV caches this request's values held, which stay here: a stand-in
        # crosses in each one's place (`controls.withhold`).
        self.held: list = []

    def pump(self, connection) -> "tuple[bytes, Optional[float]]":
        """Service the runner until it reports the block finished.

        Returns ``(saved-values blob, deserialize_ms)``. The blob is already a
        ``torch.save`` of what the block kept, ready to upload as-is.

        Raises:
            RunnerError: the block failed, carrying the traceback the runner
                formatted (tracebacks don't survive serialization).
        """
        # Set once, here, because this is where a host takes charge of a runner:
        # every tensor the runner sends is then rebuilt straight onto one of this
        # host's cards (`landing` says which). It matters because the processes on
        # this socket do not share a device -- one runner serves a whole
        # tensor-parallel group, so a tensor rebuilt from its bytes would otherwise
        # arrive on the sender's card and be mixed into a different rank's forward.
        # Doing it in the deserializer, not in a walk over the message afterwards,
        # keeps a tensor from being allocated on the sender's card first, which
        # under tensor parallelism is another rank's memory budget.
        connection.map_location = landing(self.model)
        # A line the block printed and a failure of the block can arrive at any
        # point, so the link deals with them wherever this host is waiting:
        # callers waiting on a specific reply don't have to untangle them.
        link = Link(connection, always={Print: self._log, Failed: self._fail})
        while True:
            message = link.receive(Interleave, End)
            if isinstance(message, End):
                return message.saved, message.deserialize_ms
            self.interleave(link, message)

    def _log(self, message) -> None:
        self._on_log(message.text)

    def _fail(self, message) -> None:
        raise RunnerError(message.text, message.cause_type)

    # -- envoy/device helpers ------------------------------------------------

    def _to_device(self, data):
        """Move every tensor in ``data`` onto the model's device (no-op off-GPU)."""
        device = self.model.device
        if device is None:
            return data
        return apply(data, lambda tensor: tensor.to(device), torch.Tensor)

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
        ``Barrier.__call__`` — each a `Reply`→`Park` round trip that adopts the
        worker's next park. The registry entry is cleared *before* the resumes,
        as the base empties ``_waiting``, so a released worker that reaches the
        same barrier again starts a fresh round (adopt re-enters here).

        Works the same wherever the arrival lands: an initial park in the
        `Interleave` message (every block reaches the barrier before touching the
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

    def _build_proxies(self, link, parks, batch_groups):
        """One :class:`MediatorProxy` per runner worker, each scoped to its batch
        rows, with any initial control request answered before the forward runs
        (so ``handle`` only ever sees model locations)."""
        proxies = [
            MediatorProxy(mediator_id, link, self, park)
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

    def interleave(self, link, message) -> None:
        """Run the method an `Interleave` names, interleaved with the runner's workers.

        One ``MediatorProxy`` per worker drives it over the socket as the forward
        pass reaches its locations; reads/swaps flow through exactly as nnsight's
        local mediators would. After the run, dangling workers are surfaced and the
        model's result is shipped to the process to return to the client.
        """
        interleaver = self.model.interleaver
        # Fresh barrier rounds per trace: a barrier's id is a runner-process
        # address, which a later trace in the same session could reuse. The
        # armed recursive-source paths are per-trace for the same reason.
        self._barriers = {}
        self.armed = set()
        proxies = self._build_proxies(link, message.parks, message.batch_groups)
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
                # parks: `controls.source` armed them in `_build_proxies`, before
                # this entry, and `Interleaver.__enter__` clears `sourced`.
                # A mid-run arm (settle_control after a switch) lands after the
                # clear and stays put on its own.
                for armed in self.armed:
                    interleaver.sourced.setdefault(armed, None)
                fn = getattr(self.model, message.fn)
                interleaver.batcher, args, kwargs = self._assemble(
                    fn, message.invokes, message.kwargs
                )
                result = fn(*args, **kwargs)
                # Serve the return value to any worker parked on `tracer.result`.
                interleaver.handle("result", result)
            self.check_dangling(link, proxies)
        finally:
            # Leave the interleaver clean so the next run starts fresh.
            interleaver.mediators = []
            interleaver.batcher = None
        try:
            link.send(Done(withhold(result, self.held)))
        except OSError:
            # A fatal `Throw` (check_dangling) makes the runner unwind, report
            # `Failed` and close its socket — possibly before this send. The
            # report is already buffered for `pump` to read; raising the broken
            # send instead would mask the user's real error with a
            # BrokenPipeError (#280's shape).
            pass

    def check_dangling(self, link, proxies) -> None:
        """Surface workers still parked after the run.

        Classifies each dangling park the way nnsight's ``dangling_unwind``
        does, event first — the `Throw` carries the kind (a `Dangling`) and the
        runner builds the matching error:

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
                kind = Dangling.BARRIER
            elif proxy.iteration != 0:
                kind = Dangling.ITER
            else:
                kind = Dangling.OUT_OF_ORDER
            try:
                link.send(Throw(proxy.id, str(proxy.pending), kind))
            except OSError:
                # A fatal kind already thrown makes the runner unwind, report
                # `Failed` and close its socket, racing the rest of this loop.
                # Its report is buffered for `pump` to read; stop sending so a
                # BrokenPipeError can't mask the user's real error (#280's
                # shape).
                return
