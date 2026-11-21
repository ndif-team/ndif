"""The worker's side of the loop, in the runner.

The interleaver is split across the socket at the mediator level:

* The intervention **workers** (greenlets) run in this process. A worker still
  parks on a location, but instead of resolving the occurrence here it sends the
  raw location plus its current ``tracer.iter`` pin to the host
  (``adapters.ipc_event``).
* Each worker's **parent side** — the occurrence counter, the pin, and the
  read/swap matching — lives on the host as one proxy per worker
  (``driver.MediatorProxy``). The host drives each worker individually over the
  socket: `IPCInterleaver.pump` switches a worker in on ``Resume`` and answers
  with its next park, throws the host-classified dangling error on ``Throw``,
  and ends on ``Done``.

So iteration state is tracked per mediator on the host, and a worker's activation
values only cross when the host resumes it (a read) or it swaps (in its park).
Because a read hands back a *copy*, ``pump`` sends each read's value back after
the worker moves past it, so in-place edits (``output[:] = 0``) reach the host.
"""

import warnings

from nnsight.intervention.cache import Cache
from nnsight.intervention.envoy import Envoy
from nnsight.intervention.interleaver import (
    EarlyStopException,
    Event,
    Interleaver,
    OutOfOrderError,
)

from .messages import CacheHit, Dangling, Done, Park, Reply, Resume, Stop, Throw, Writeback


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
