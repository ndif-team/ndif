"""Tests for split autograd on the untrusted path.

Each socket crossing links the host and runner graphs (``sandbox/autograd.py``):
the runner backpropagates the loss to the values it read, the host continues from
there, and any runner tensor sent to the model calls back into the runner for
its gradients. Each equivalence test runs the same block in process (trusted
path) and in a runner (untrusted path) and checks ``torch.equal``.

Uses the same stack-free harness as ``test_sandbox_iter_pins``: a CPU model, a
runner over a Unix socket, and a ``SandboxDriver`` driving it. Each block sets
``out = None`` before the trace so the capture run returns ``None`` while the
trusted run returns the saved dict.
"""

from __future__ import annotations

import gc

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from test_sandbox_iter_pins import REPO, Capture, run_sandboxed

from ndif.services.ray.sandbox.autograd import HostGraph
from ndif.services.ray.sandbox.driver import RunnerError

PROMPT = "The Eiffel Tower is in the city of"
OTHER = "Madison Square Garden is located in"
LLAMA = "hf-internal-testing/tiny-random-LlamaForCausalLM"


@pytest.fixture(autouse=True)
def float32_default():
    # The runner starts with float32 default dtype. `ModelEvaluator.__init__`
    # sets bfloat16 process-wide, which would otherwise make in-process blocks
    # create bfloat16 tensors after a controller test.
    before = torch.get_default_dtype()
    torch.set_default_dtype(torch.float32)
    yield
    torch.set_default_dtype(before)


def load(repo):
    from nnsight import TransformersModel

    try:
        model = TransformersModel(repo, task="text-generation", device="cpu", dispatch=True)
    except Exception as error:  # no cached weights and no network, most likely
        pytest.skip(f"could not load {repo}: {error}")
    # Frozen, as the server loads it (`BaseModelDeployment.load_from_disk`).
    model._module.requires_grad_(False)
    return model


@pytest.fixture(scope="module")
def model():
    return load(REPO)


@pytest.fixture(scope="module")
def llama():
    return load(LLAMA)


def untrusted(model, block):
    capture = Capture()
    block(model, capture)
    return run_sandboxed(model, capture.blob)["out"]


def assert_matches_trusted(model, block):
    expected = block(model, None)
    saved = untrusted(model, block)
    assert expected.keys() == saved.keys()
    for name, value in expected.items():
        assert torch.equal(saved[name], value), name


# ------------------------------------------------------------------ blocks


def read_grad(model, backend):
    out = None
    with model.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        act = model.transformer.h[1].output
        act.requires_grad_(True)
        loss = model.output.logits.sum()
        with loss.backward():
            out["grad"] = act.grad
    return out


def read_grad_after_bare_backward(model, backend):
    out = None
    with model.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        act = model.transformer.h[1].output
        act.requires_grad_(True)
        model.output.logits.sum().backward()
        out["grad"] = act.grad
    return out


def backward_right_after_requires_grad(model, backend):
    # Start backward with no intervening park after `requires_grad_()`.
    out = None
    with model.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        act = model.transformer.h[3].output
        act.requires_grad_(True)
        loss = act.pow(2).sum()
        with loss.backward():
            out["grad"] = act.grad
    return out


def edit_grad(model, backend):
    out = None
    with model.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        early = model.transformer.h[0].output
        early.requires_grad_(True)
        late = model.transformer.h[2].output
        loss = model.output.logits.sum()
        with loss.backward():
            late.grad = late.grad * 2
            out["early_grad"] = early.grad
    return out


def steering_vector(model, backend):
    out = None
    with model.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        hidden = model.transformer.h[1].output
        vector = torch.zeros(768, requires_grad=True)
        hidden[:] = hidden + vector
        model.output.logits[0, -1].logsumexp(-1).backward()
        out["vector_grad"] = vector.grad
    return out


def edit_grad_of_steered_read(model, backend):
    # Edit `.grad` inside a backward block for a read tensor edited in place.
    out = None
    with model.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        hidden = model.transformer.h[1].output
        vector = torch.full((768,), 0.3, requires_grad=True)
        hidden[:] = hidden + vector
        loss = model.output.logits[0, -1].logsumexp(-1)
        with loss.backward():
            hidden.grad = hidden.grad * 4
        out["vector_grad"] = vector.grad
    return out


def training_loop(model, backend):
    out = None
    with model.session(backend=backend):
        out = nnsight.save({})
        delta = torch.zeros(768, requires_grad=True)
        optimizer = torch.optim.AdamW([delta], lr=0.05)
        for step in range(3):
            with model.trace(PROMPT):
                hidden = model.transformer.h[2].output
                model.transformer.h[2].output = hidden + delta
                loss = -model.output.logits[0, -1].log_softmax(-1)[3]
                loss.backward()
            optimizer.step()
            optimizer.zero_grad()
        out["delta"] = delta.detach()
    return out


def batched_invokes(model, backend):
    out = None
    with model.trace(backend=backend) as tracer:
        out = nnsight.save({})
        with tracer.invoke(PROMPT):
            hidden = model.transformer.h[1].output
            first = torch.zeros(768, requires_grad=True)
            hidden[:] = hidden + first
            late = model.transformer.h[3].output
            loss = model.output.logits[0, -1].logsumexp(-1)
            with loss.backward(retain_graph=True):
                out["first_late"] = late.grad
            out["first_grad"] = first.grad
        with tracer.invoke(OTHER):
            hidden = model.transformer.h[1].output
            second = torch.full((768,), 0.01, requires_grad=True)
            hidden[:] = hidden + second
            late = model.transformer.h[3].output
            loss = model.output.logits[0, -1].logsumexp(-1)
            with loss.backward(retain_graph=True):
                out["second_late"] = late.grad
            out["second_grad"] = second.grad
    return out


def second_backward_through_a_swapped_read(model, backend):
    # Both host backwards call back into the runner through the swap to reach
    # `act`, so its gradient accumulates across both passes.
    out = None
    with model.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        act = model.transformer.h[1].output
        act.requires_grad_(True)
        model.transformer.h[1].output = act * 1.5
        loss = model.output.logits[0, -1].logsumexp(-1)
        loss.backward(retain_graph=True)
        loss.backward(retain_graph=True)
        out["grad"] = act.grad
    return out


def reset_grad_between_backwards(model, backend):
    out = None
    with model.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        act = model.transformer.h[1].output
        act.requires_grad_(True)
        loss = model.output.logits[0, -1].logsumexp(-1)
        loss.backward(retain_graph=True)
        act.grad = None
        (loss * 3).backward()
        out["grad"] = act.grad
    return out


def steer_a_keyword_input(llama, backend):
    # Llama attention takes its hidden states as a keyword argument, so the host
    # writes the edit back through `write_back`'s dict branch; a zero vector
    # leaves the values unchanged.
    out = None
    with llama.trace(PROMPT, backend=backend):
        out = nnsight.save({})
        hidden = llama.model.layers[0].self_attn.input
        vector = torch.zeros(hidden.shape[-1], requires_grad=True)
        hidden[:] = hidden + vector
        llama.output.logits[0, -1].logsumexp(-1).backward()
        out["vector_grad"] = vector.grad
    return out


@pytest.mark.parametrize(
    "block",
    [
        read_grad,
        read_grad_after_bare_backward,
        backward_right_after_requires_grad,
        edit_grad,
        steering_vector,
        edit_grad_of_steered_read,
        training_loop,
        batched_invokes,
        second_backward_through_a_swapped_read,
        reset_grad_between_backwards,
    ],
    ids=lambda block: block.__name__,
)
def test_matches_the_trusted_path(model, block):
    assert_matches_trusted(model, block)


def test_steering_a_keyword_input_matches_the_trusted_path(llama):
    assert_matches_trusted(llama, steer_a_keyword_input)


# ------------------------------------------------------------------ refusals


def refusal(model, block) -> RunnerError:
    with pytest.raises(RunnerError) as caught:
        untrusted(model, block)
    assert caught.value.cause_type == "NotImplementedError"
    return caught.value


def test_autograd_grad_is_refused(model):
    def block(model, backend):
        with model.trace(PROMPT, backend=backend):
            act = model.transformer.h[0].output
            act.requires_grad_(True)
            nnsight.save(torch.autograd.grad(model.output.logits.sum(), act))

    assert "torch.autograd.grad is not supported" in str(refusal(model, block))


def test_a_backward_across_traces_is_refused(model):
    # The first trace's host graph is released before the session body runs.
    def block(model, backend):
        with model.session(backend=backend):
            vector = torch.full((768,), 0.3, requires_grad=True)
            with model.trace(PROMPT):
                hidden = model.transformer.h[1].output
                hidden.requires_grad_(True)
                loss = (model.output.logits[0, -1] * vector[0]).logsumexp(-1)
            loss.backward()
            nnsight.save(vector.grad)

    assert "earlier trace" in str(refusal(model, block))


def test_a_backward_in_a_later_trace_is_refused(model):
    # Backward runs at the second trace's first park, before any read.
    def block(model, backend):
        with model.session(backend=backend):
            vector = torch.full((768,), 0.2, requires_grad=True)
            with model.trace(PROMPT):
                hidden = model.transformer.h[1].output
                hidden[:] = hidden + vector
                loss = model.output.logits[0, -1].logsumexp(-1)
            with model.trace(OTHER):
                loss.backward()
            nnsight.save(vector.grad)

    assert "earlier trace" in str(refusal(model, block))


def test_a_backward_inside_a_backward_block_is_refused(model):
    def block(model, backend):
        with model.trace(PROMPT, backend=backend):
            act = model.transformer.h[1].output
            act.requires_grad_(True)
            loss = model.output.logits.sum()
            with loss.backward(retain_graph=True):
                nnsight.save(act.grad)
                loss.backward()

    assert "inside a `with loss.backward():` block" in str(refusal(model, block))


def test_no_host_graph_outlives_its_request(model):
    # `FromRunner` contexts reference their `HostGraph`; steering creates one,
    # and the cycle must not keep served forward tensors alive after the run.
    untrusted(model, steering_vector)
    gc.collect()
    assert not [o for o in gc.get_objects() if type(o) is HostGraph and o.served]
