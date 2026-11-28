"""What a host and a runner say to each other: one class per message.

Both ends import this module, so the classes are the catalog. A message crosses
the socket whole, as one object (``protocol.encode``), and the receiver tells
messages apart by type (``link.Link``).

A request is three conversations, one inside the next:

* the request: the host sends `Payload`; the runner answers `End` or `Failed`;
* a run, once for each call of the model: the runner sends `Interleave`; the
  host answers `Done`;
* an exchange with one worker: the host sends `Resume`; the runner answers with
  the worker's next `Park`, or with `Stop`.

During a host backward (`Backward`), the host sends `GradRequest` when it
reaches a tensor from the runner, and the runner replies with `GradReply`.

A worker that needs something only the host can do parks on a `Control` request
in place of a location. The host answers it with a `Reply` and the exchange goes
on. `Print` and `CacheHit` answer nothing and can arrive at any point.

Imports nothing from nnsight or torch, so a test can script either end.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Optional

# ------------------------------------------------------------------ host -> runner


@dataclass
class Payload:
    """The request. The first message; starts the block.

    ``dtype`` is the model's, sent because the runner has no loaded model to ask.
    ``seed`` is ``None`` unless several processes must draw the same numbers, as
    tensor-parallel ranks do. ``env`` is the request's environment, applied to
    the runner's meta model so its tree matches the host's.
    """

    blob: bytes
    compress: bool
    dtype: str
    seed: Optional[int] = None
    env: Optional[dict] = None


@dataclass
class Resume:
    """Switch a worker in: the answer to a park on a model location.

    ``args`` is ``(value,)`` for a read and ``()`` for a swap or a skip. ``pin``
    is the host's ``tracer.iter`` pin for the worker, which the runner adopts so
    that both ends relax it together.
    """

    worker: int
    args: tuple
    pin: Optional[int]
    #: Per-tensor ``(crossing, requires_grad)`` list for ``args`` (``autograd.py``).
    crossings: Any = None


@dataclass
class Reply:
    """Switch a worker in: the answer to a `Control` request or a barrier park.

    Unlike `Resume` it carries no pin. Neither kind of park sent one, and the
    worker may have moved its own since its last model park (a ``tracer.iter``
    step), so the runner leaves the worker's pin as it is (#296).
    """

    worker: int
    args: tuple = ()
    #: Same format as `Resume.crossings`.
    crossings: Any = None


class Dangling(enum.Enum):
    """Why a worker was still parked when the run ended."""

    #: The model ran past the location: ``OutOfOrderError`` in the worker.
    OUT_OF_ORDER = "out of order"
    #: A ``tracer.iter`` loop outran the model: unwound with a warning.
    ITER = "iter"
    #: Fewer blocks reached a barrier than it waits for: ``ValueError``.
    BARRIER = "barrier"


@dataclass
class Throw:
    """The run ended with this worker parked on ``requester``. No reply."""

    worker: int
    requester: str
    kind: Dangling


@dataclass
class CacheHit:
    """One value for a ``tracer.cache()`` that lives in the runner. No reply."""

    cache: int
    path: str
    key: str
    value: Any


@dataclass
class Done:
    """The call of the model returned ``result``. Ends a run."""

    result: Any


@dataclass
class GradRequest:
    """Host -> runner: backpropagate ``grad`` through the tensor sent under
    ``out``. Answered by `GradReply`."""

    out: int
    grad: Any


# ------------------------------------------------------------------ runner -> host


@dataclass
class Interleave:
    """Run the model's method ``fn``, interleaved with this trace's workers.

    ``parks`` holds each worker's first park and ``batch_groups`` its rows of
    the batch. ``invokes`` are the raw inputs of each invoke, which the host's
    tokenizer assembles. ``kwargs`` are the call's own.
    """

    fn: str
    parks: list
    batch_groups: list
    invokes: list
    kwargs: dict


@dataclass
class Park:
    """Where a worker waits next, in answer to `Resume` or `Reply`.

    ``pending`` is nnsight's ``Pending`` for a model location or a barrier, a
    `Control` request, or ``None`` when the worker has finished. A ``Pending``
    carries the worker's pin in its ``iteration`` field: the host owns the
    count of visits and resolves the occurrence.

    ``edited`` is set only in answer to a `Resume` that served a read, and only
    when the block edited the value in place: it is the worker's copy of the
    value, which the host writes into the one it served. ``None`` means there is
    nothing to write.
    """

    worker: int
    pending: Any
    edited: Any = None
    #: Per-tensor ``None`` or ``(out_id, reached_crossings)`` for
    #: ``(pending, edited)``, linking runner tensors into the host graph
    #: (``autograd.py``).
    links: Any = None
    #: Crossings where the block called ``requires_grad_()``.
    mirror: Any = None


@dataclass
class GradReply:
    """Runner -> host: gradients for the crossings reached by the requested
    tensor, in the order they were sent."""

    grads: list


@dataclass
class Stop:
    """A worker called ``tracer.stop()``: unwind the model. Sent in place of a `Park`."""


@dataclass
class Print:
    """One line the block wrote to stdout or stderr."""

    text: str


@dataclass
class End:
    """The block finished. ``saved`` is the ``torch.save`` of what it kept."""

    saved: bytes
    deserialize_ms: Optional[float] = None


@dataclass
class Failed:
    """The block raised. ``text`` is the traceback, formatted in the runner
    because tracebacks do not survive the socket. ``cause_type`` is the name of
    the exception's class, which telemetry records (#280)."""

    text: str
    cause_type: Optional[str] = None


# ------------------------------------------------------------------ control requests


class Control:
    """A park that asks the host for something, in place of a model location.

    A worker parks on one the way it parks on a location, and it rides in
    `Park`. The three attributes are the ones nnsight's interleaver reads from
    any park, set so that a request never matches a location.
    """

    event = provider = iteration = None


@dataclass
class Source(Control):
    """Instrument the module at ``path`` and describe its forward, for ``.source``.

    ``path`` may be the path of an operation instead
    (``...attn.source.attention_interface_1``). That arms the host to describe
    the function the operation calls when the forward reaches it (#281).
    """

    path: str


@dataclass
class Call(Control):
    """Run the module at ``path`` on these arguments; the reply is its output (#295)."""

    path: str
    hook: bool
    args: tuple
    kwargs: dict


@dataclass
class Observe(Control):
    """Watch the forward for a ``tracer.cache()``. Each value the cache keeps
    comes back as a `CacheHit`."""

    cache: int
    config: dict


@dataclass
class SourceRefused:
    """Served in place of a description when a function cannot be drilled into.

    The runner raises ``SourceNotAvailable(message)`` from the user's own frame,
    as the trusted path does (#312).
    """

    message: str


@dataclass
class Backward(Control):
    """Control request: run the host's half of a backward started in the runner.

    ``roots`` are crossing ids and ``grads`` are their gradients from the
    runner's half. For a backward block (``with loss.backward():``), ``worker``
    is the block's runner worker id and ``first`` is its first park; for a bare
    ``backward()``, ``worker`` is ``None``.
    """

    roots: list
    grads: list
    worker: Optional[int] = None
    first: Any = None
    retain_graph: Optional[bool] = None


@dataclass
class WatchGrad(Control):
    """Control request: hook the gradient of the host tensor under ``crossing``
    so a backward block can read or write its ``.grad``."""

    crossing: int
