"""The untrusted path takes no backward passes, and says why.

The runner has no copy of the model's graph, so a backward through the model
gets no gradients from it. Left alone that was opaque or silent: ``with
loss.backward(): act.grad`` dangled into ``OutOfOrderError:
'140075482858912.grad.i0' was requested but the model already ran past it``, and
a bare ``loss.backward()`` finished with every ``.grad`` left ``None``. The runner
now replaces ``Tensor.backward``, ``torch.autograd.backward`` and
``torch.autograd.grad`` with a function from ``adapters.refusal``, which raises
``NotImplementedError(adapters.NO_BACKWARD)``.

Same stack-free harness as ``test_sandbox_iter_pins``: a real gpt2 on CPU, a
runner spawned over a Unix socket, a ``SandboxDriver`` pumping it.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from test_sandbox_iter_pins import REPO, Capture, run_sandboxed

from ndif.services.ray.sandbox.driver import RunnerError

PROMPT = "The Eiffel Tower is in the city of"


@pytest.fixture(scope="module")
def model():
    from nnsight import TransformersModel

    try:
        model = TransformersModel(
            REPO, task="text-generation", device="cpu", dispatch=True
        )
    except Exception as error:  # no cached weights and no network, most likely
        pytest.skip(f"could not load {REPO}: {error}")
    # Frozen, as the server loads it (`BaseModelDeployment.load_from_disk`).
    model._module.requires_grad_(False)
    return model


def trace(model, backend):
    return model.trace(PROMPT, **({"backend": backend} if backend else {}))


def backward_trace(model, backend):
    with trace(model, backend):
        act = model.transformer.h[0].output
        act.requires_grad_(True)
        loss = model.output.logits.sum()
        with loss.backward():
            act.grad.save()


def bare_backward(model, backend):
    # Before the refusal this one finished, with `grad` None.
    with trace(model, backend):
        act = model.transformer.h[0].output
        act.requires_grad_(True)
        act.retain_grad()
        model.output.logits.sum().backward()
        nnsight.save(act.grad)


def steering_vector(model, backend):
    # Likewise: finished, with `vector.grad` None.
    with trace(model, backend):
        vector = torch.zeros(768, requires_grad=True)
        model.transformer.h[0].output = model.transformer.h[0].output + vector
        model.output.logits.sum().backward()
        nnsight.save(vector.grad)


def autograd_grad(model, backend):
    with trace(model, backend):
        act = model.transformer.h[0].output
        act.requires_grad_(True)
        nnsight.save(torch.autograd.grad(model.output.logits.sum(), act))


def probe_on_detached(model, backend):
    # Never touches the model's graph, and is refused all the same: no backward
    # passes on this path.
    with trace(model, backend):
        hidden = model.transformer.h[-1].output.detach()
        weight = torch.ones(768, requires_grad=True)
        (hidden @ weight).sum().backward()
        nnsight.save(weight.grad)


@pytest.mark.parametrize(
    "block",
    [backward_trace, bare_backward, steering_vector, autograd_grad, probe_on_detached],
    ids=lambda block: block.__name__,
)
def test_refused_with_the_reason(model, block):
    capture = Capture()
    block(model, capture)
    with pytest.raises(RunnerError) as caught:
        run_sandboxed(model, capture.blob)
    assert caught.value.cause_type == "NotImplementedError"
    assert "ask the maintainers of this NDIF server for trusted access" in str(
        caught.value
    )
