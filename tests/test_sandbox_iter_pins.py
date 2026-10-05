"""The `tracer.iter` pin survives control events on the sandbox path (#296).

A worker's `tracer.iter` pin lives in the runner and advances between parks; the
host pushes its own copy back on every RESUME so relaxation stays in lockstep.
A *control* park (SOURCE/CALL/CACHE) carries no pin — and `.source` parks a
SOURCE on every access, so a loop body that touches `.source` parks one right
after the loop advanced the pin. A RESUME that answered it by pushing the host's
stale copy wound the worker back to the previous step. From there, two faces of
one bug:

* with a later read in the body (the issue's repro), the rewound source-op read
  asked for an occurrence the model was already past, dangled to the end of the
  run, and the host's dangling check raised
  ``OutOfOrderError: '...source.self_act_0.output.i0' was requested but the
  model already ran past it``;
* without one, the rewound read matched the visit the host was still serving and
  was handed the *previous step's value* again — three identical "steps", no
  error, silently wrong.

The fix is the ``KEEP_PIN`` sentinel on control-event RESUMEs
(``MediatorProxy.settle_control``), which the runner's pump treats as "leave the
worker's pin alone".

No server and no Ray: a real gpt2 on CPU, a runner spawned over a Unix socket,
and a ``SandboxDriver`` pumping it — the same halves a request gets, minus the
actor. The trusted path is the ground truth, so each test runs the identical
block locally first and compares values step by step; "no exception" alone would
have passed the silent variant before the fix. Unlike ``test_fanout.py`` this
needs nnsight and the gpt2 weights, so it skips without them rather than running
anywhere.
"""

from __future__ import annotations

import io

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from ndif.services.ray.sandbox import host
from ndif.services.ray.sandbox.driver import SandboxDriver

REPO = "openai-community/gpt2"
PROMPT = "The Eiffel Tower is in the city of"
# Held to exactly three steps (min_new_tokens) so a loop over tracer.iter[:3]
# never legitimately outruns the run — any shortfall is the bug, not an EOS.
GENERATE = dict(max_new_tokens=3, min_new_tokens=3, do_sample=False)


@pytest.fixture(scope="module")
def model():
    """A real, dispatched gpt2 on CPU: this process is the host, so it holds the
    weights the driver runs. CPU keeps the test off the GPU entirely."""
    from nnsight import TransformersModel

    try:
        return TransformersModel(
            REPO, task="text-generation", device="cpu", dispatch=True
        )
    except Exception as error:  # no cached weights and no network, most likely
        pytest.skip(f"could not load {REPO}: {error}")


class Capture:
    """A backend that serializes the trace exactly as the remote path would and
    runs nothing — the client half of a request, without a client."""

    def __init__(self):
        self.blob = None

    def __call__(self, tracer):
        from nnsight.schema.request import RequestModel

        self.blob = RequestModel.serialize(tracer, False)


def run_sandboxed(model, blob: bytes):
    """Drive ``blob`` through a real runner process and return its saved values."""
    # The full "import.path.Class:repo" key, as the actor's pool passes it, so
    # the runner builds its meta model and the payload's persistent ids resolve
    # the way they do in production (a bare repo id degrades to the
    # no-meta-model path, which still works but isn't what a request gets).
    sandbox = host.spawn(model_key=model.to_model_key())
    try:
        connection = sandbox.connection()
        # The payload message run_in_runner sends: blob, compress, dtype, seed, env.
        connection.send((blob, False, "torch.float32", None, {}))
        data, _ = SandboxDriver(model, torch.float32).pump(connection)
        return torch.load(io.BytesIO(data), weights_only=False)
    finally:
        sandbox.stop()


def both_ways(model, block):
    """Run ``block`` locally (ground truth) and sandboxed; return both acts lists.

    ``block`` takes ``(tracer, acts, mlp)`` and appends one tensor per step.
    """
    mlp = model.transformer.h[2].mlp

    with model.generate(PROMPT, **GENERATE) as tracer:
        acts = nnsight.save([])
        block(tracer, acts, mlp)
    local = [tensor.detach().clone() for tensor in acts]

    capture = Capture()
    with model.generate(PROMPT, backend=capture, **GENERATE) as tracer:
        acts = nnsight.save([])
        block(tracer, acts, mlp)

    saved = run_sandboxed(model, capture.blob)
    return local, saved["acts"]


def assert_steps_match(local, sandboxed):
    assert len(sandboxed) == len(local) == 3
    for step, (expected, actual) in enumerate(zip(local, sandboxed)):
        assert torch.allclose(actual, expected), (
            f"step {step} diverged from the trusted run: {actual} != {expected}"
        )


class TestIterLoopsOverControlEvents:
    """A `.source` touch mid-loop must not move the loop's pin."""

    def test_a_source_op_then_the_modules_output(self, model):
        # The issue's repro: before the fix this raised OutOfOrderError on
        # '...source.self_act_0.output.i0' out of the dangling check.
        def block(tracer, acts, mlp):
            for step in tracer.iter[:3]:
                acts.append(mlp.source.self_act_0.output[0, -1, :4])
                mlp.output

        assert_steps_match(*both_ways(model, block))

    def test_a_source_op_alone(self, model):
        # The silent face: before the fix this *completed* with step 0's value
        # served three times, so the assertion has to compare values, not just
        # finish without an error.
        def block(tracer, acts, mlp):
            for step in tracer.iter[:3]:
                acts.append(mlp.source.self_act_0.output[0, -1, :4])

        assert_steps_match(*both_ways(model, block))

    def test_a_plain_iter_loop_still_locks_step(self, model):
        # No control events at all: the pin push on value RESUMEs — what KEEP_PIN
        # must *not* disturb — is the only thing keeping this loop in step.
        def block(tracer, acts, mlp):
            for step in tracer.iter[:3]:
                acts.append(mlp.output[0, -1, :4])

        assert_steps_match(*both_ways(model, block))
