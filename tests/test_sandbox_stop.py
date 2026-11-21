"""``tracer.stop()`` in a sandboxed block ends the run it is in and nothing else.

The runner told the host to stop and left its loop at once. The host had not
finished: it still serves the other workers parked on the visit the stop fired
at, and it sends its ``Done`` after every run. Nothing read those. So:

* in a session, the next trace read the stale ``Done`` first, returned with
  nothing served, and the host then took a later ``Interleave`` for a park
  (#362);
* with two invokes, the host resumed the second worker and was answered by
  whatever the runner sent next.

The runner now holds the stop until the host's ``Done`` and answers what the
host sends in between, as nnsight's own interleaver does.

Same harness as ``test_sandbox_iter_pins``, whose helpers this imports: no server
and no Ray, a real gpt2 on CPU, a runner spawned over a Unix socket and a
``SandboxDriver`` pumping it. The trusted run is the ground truth.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from test_sandbox_iter_pins import (  # noqa: F401  (model is a fixture)
    PROMPT,
    Capture,
    model,
    run_sandboxed,
)


def test_the_trace_after_a_stopped_one_is_served(model):
    # The issue's shape: one session, three traces, the first one stops.
    block = model.transformer.h[0]

    with model.session():
        with model.trace(PROMPT) as tracer:
            first = block.output.save()
            tracer.stop()
        with model.trace(PROMPT):
            second = block.output.save()
        with model.trace(PROMPT):
            third = model.lm_head.output.save()
    local = [first, second, third]

    capture = Capture()
    with model.session(backend=capture):
        with model.trace(PROMPT) as tracer:
            first = block.output.save()
            tracer.stop()
        with model.trace(PROMPT):
            second = block.output.save()
        with model.trace(PROMPT):
            third = model.lm_head.output.save()

    saved = run_sandboxed(model, capture.blob)
    for name, expected in zip(("first", "second", "third"), local):
        assert torch.allclose(saved[name], expected), f"{name} differs from the trusted run"


def test_a_stop_still_serves_the_other_invoke_at_that_location(model):
    block = model.transformer.h[0]

    with model.trace() as tracer:
        with tracer.invoke(PROMPT):
            stopped = block.output.save()
            tracer.stop()
        with tracer.invoke(PROMPT):
            other = block.output.save()
    local = [stopped, other]

    capture = Capture()
    with model.trace(backend=capture) as tracer:
        with tracer.invoke(PROMPT):
            stopped = block.output.save()
            tracer.stop()
        with tracer.invoke(PROMPT):
            other = block.output.save()

    saved = run_sandboxed(model, capture.blob)
    for name, expected in zip(("stopped", "other"), local):
        assert torch.allclose(saved[name], expected), f"{name} differs from the trusted run"
