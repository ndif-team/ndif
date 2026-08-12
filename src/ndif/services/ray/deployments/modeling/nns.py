"""Running a user's traced block against a loaded model.

The nnsight-facing half of a request: rebuild the tracer the client serialized,
run the block interleaved with the model's forward, and collect the values it
marked with ``nnsight.save()``. Everything around it — statuses, timeouts,
metrics, uploads — belongs to the actor in `base.py`.

Separate from that actor for two reasons. It is the one part of a request the
**tensor-parallel shards** also run, and they must run it *identically* — the
autocast dtype in particular, since ranks that disagree would reduce tensors of
different dtypes. And a shard has no use for the actor's world: importing
`base.py` would pull Ray, boto3 and influxdb_client into a process that only ever
runs a block, and connect to the object store on the way past.

Building the block and running it are separate calls because a tensor-parallel
group needs to get between them: every rank proves it can *build* the block
before any rank starts a forward pass, since deserializing is where a request
usually fails and the last point where one can fail safely (see
`ray/tp/host.py`). Every caller makes both calls, single-process ones included:
the base actor's `commit()` sits between them.
"""

import contextlib
import linecache
import time
from typing import Optional

import torch

from nnsight.tracing.globals import BLOCKS, SOURCES
from nnsight.tracing.tracer import _saves, dec, inc

from .....common.telemetry import elapsed_ms


@contextlib.contextmanager
def block_scope():
    """Confine one request's source-cache mutations to that request.

    Deserializing a block registers its source in the global ``linecache`` under
    the *client's* filename, and nnsight memoizes the parsed and compiled block
    in process-global ``SOURCES``/``BLOCKS``, keyed by filename and line and
    never re-validated. None of that is cleaned up on its own, so on a long-lived
    process it accumulates and the next request is the one that pays.

    Two ways it goes wrong, and the second is why this is shared rather than
    living in the actor:

    * A later request reusing a ``(filename, line)`` trace site runs the *stale*
      block memoized there.
    * A client sends only the block's own source, so ``linecache`` ends up
      holding a fragment of their file. A second trace elsewhere in the same file
      then parses that fragment, finds no ``with`` at its line, and raises
      ``WithBlockNotFoundError`` — which on a tensor-parallel replica means the
      shards drop out of the forward while rank 0 waits in a collective for the
      execution timeout to fire. Reproduced with two traces in one test file.

    Every process that builds a block needs this, which is both the actor and the
    shards. Getting it in only one of them is what produced the failure above.
    """
    linecache_snapshot = dict(linecache.cache)
    sources_snapshot = dict(SOURCES)
    blocks_snapshot = dict(BLOCKS)
    try:
        yield
    finally:
        linecache.cache.clear()
        linecache.cache.update(linecache_snapshot)
        SOURCES.clear()
        SOURCES.update(sources_snapshot)
        BLOCKS.clear()
        BLOCKS.update(blocks_snapshot)


def request_dtype(dtype):
    """The autocast region a request's work runs inside.

    Autocasts tensors the user's code creates to the model's dtype, matching how
    the weights were loaded. Only meaningful for half precision; a no-op
    (``enabled=False``) otherwise, and when there is no CUDA device.

    ``cache_enabled=False`` because this region spans the *whole* request, not
    one forward pass. Autocast's cache keeps the half-precision copy it made of
    each fp32 leaf that requires grad and reuses it for the rest of the region —
    correct for a single forward, and silently wrong for a session that trains. A
    block that creates parameters and steps an optimizer (the LoRA tutorial) then
    runs every forward after the first against the snapshot taken before the
    first ``optimizer.step()``: the gradients are real and the weights do move,
    but the model never sees the new values, so the loss doesn't respond and
    nothing appears to train. Disabling the cache costs nothing here — the served
    weights are already loaded in ``dtype``, so there is no weight cast to reuse.

    Its own function because **three** places need the identical region and they
    are in different processes: the in-process actor, a tensor-parallel shard,
    and — separately — the sandbox host, which runs the *forward* on behalf of a
    runner whose own bracket is in another process entirely. Miss one and the
    same script computes different numbers depending on where it happened to be
    scheduled, which is exactly what happened.
    """
    autocast_enabled = torch.cuda.is_available() and dtype in (
        torch.float16,
        torch.bfloat16,
    )
    return torch.autocast(
        device_type="cuda",
        dtype=dtype,
        enabled=autocast_enabled,
        cache_enabled=False,
    )


def seed_block(seed: Optional[int]) -> None:
    """Put this process's RNG in a known state before a block runs.

    ``None`` leaves it alone, which is the ordinary single-process case: two
    identical requests are *meant* to draw differently, and seeding every request
    would silently turn repeated sampling into repeated identical samples.

    A seed is passed when several processes have to draw the *same* numbers as
    each other within one request. Under tensor parallelism that is a correctness
    requirement rather than hygiene: ranks that sample differently generate
    different tokens, and the model's own all-reduces then sum activations
    computed from different sequences, so the answer is wrong on every rank
    rather than merely inconsistent. Many checkpoints ship ``do_sample: true``,
    so it applies to requests that never asked to sample.

    Here rather than in the tensor-parallel package because the processes that
    need it no longer all live there — a sandbox runner runs the block too, and
    it must seed exactly as a rank does.
    """
    if seed is None:
        return

    import torch

    torch.manual_seed(seed)


def prepare_traced_block(model, payload, compress, deserialize):
    """Rebuild a request's tracer against this process's live model.

    Split from :func:`execute_traced_block` because a tensor-parallel deployment
    needs the two apart: its shards prove they can *build* a block before rank 0
    commits to a forward pass, since a payload that fails to deserialize is the
    common failure and the last one that can still be reported without leaving
    the other ranks blocked in a collective.

    ``deserialize`` is the intended difference between callers: the actor passes
    ``BackendRequestModel.deserialize`` (which classifies failures into
    caller-facing errors), a bare shard passes nnsight's, whose errors go home as
    text over its own channel.

    Returns ``(tracer, deserialize_ms)``.
    """
    deserialize_started = time.time()
    persistent_objects = model._remoteable_persistent_objects()
    tracer = deserialize(payload, persistent_objects, compress=compress)
    return tracer, elapsed_ms(deserialize_started)


def execute_traced_block(tracer, dtype, seed: Optional[int] = None):
    """Run an already-built block and return the values it saved.

    Autocasts user-created tensors to the model's dtype, runs the block bracketed
    in a trace scope, and collects the values it marked with ``nnsight.save()``.

    Shared with the tensor-parallel shards rather than reimplemented there,
    because every rank has to execute a block *identically* — the autocast dtype
    and the seed in particular, since ranks that disagree on either reduce
    tensors computed from different things. Two copies of this would be two
    chances to drift.
    """
    # Immediately before the block, not before deserializing it: what has to
    # match across processes is the state the *user's* code draws from.
    seed_block(seed)
    with request_dtype(dtype):
        # Bracket execution in a trace scope (inc/dec), the way the client's
        # Tracer.__exit__ does, so nnsight's save() records into this thread's
        # save set and it's cleared afterward, then collect the values the block
        # marked with nnsight.save() (by identity).
        inc()
        try:
            tracer.execute(tracer.info.code)
            saves = _saves()
            saved = {
                name: value
                for name, value in tracer.info.frame.f_locals.items()
                if id(value) in saves
            }
        finally:
            dec()

    return saved

