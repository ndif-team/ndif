"""A sandboxed block edits the host's own values, not copies of them.

A value a block reads crosses the socket as a copy, and the runner sends the copy
back when the block moves on, so in-place edits reach the forward. The host used
to swap that copy in at the location, which replaced every object in it. The
forward's other references to the originals then held something else than the
module ran on, and the same block returned different numbers than on the trusted
path, with no error:

* An in-place edit of a tensor reached only the module it was read from. The
  input of a block's first norm is the residual stream, the same tensor the block
  adds back after attention, and that sum used the unedited values.
* A read of a layer's ``.input`` carries the whole ``(args, kwargs)``, the KV
  cache included. The layer wrote the step's keys into the copy of the cache, the
  real cache never got them, and every later step of a cached ``generate`` ran
  that layer's attention without its history.

The fix is ``controls.write_back``: the copy's tensors are written into the
originals, and nothing is replaced.

The runner sends a copy back only when the block edited it (``worker.edited``),
which has to agree with what ``write_back`` would write.

Same harness as ``test_sandbox_iter_pins``, whose helpers this imports: no server
and no Ray, a real gpt2 on CPU, a runner spawned over a Unix socket and a
``SandboxDriver`` pumping it. The trusted run is the ground truth.
"""

from __future__ import annotations

import copy

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from nnsight.util import apply

from ndif.services.ray.sandbox import driver
from ndif.services.ray.sandbox.controls import write_back
from ndif.services.ray.sandbox.worker import edited, snapshot

from test_sandbox_iter_pins import (  # noqa: F401  (model is a fixture)
    PROMPT,
    Capture,
    model,
    run_sandboxed,
)

GENERATE = dict(max_new_tokens=3, min_new_tokens=3, do_sample=False, use_cache=True)


@pytest.fixture(scope="module")
def frozen(model):
    """gpt2 as the actor serves it (``load_from_disk``): no parameter requires grad.

    With gradients on, a value a block reads arrives in the runner as a leaf that
    requires grad, and torch refuses an in-place edit of one.
    """
    model._module.requires_grad_(False)
    return model


def test_an_in_place_edit_reaches_the_residual_stream(frozen):
    model = frozen
    norm = model.transformer.h[5].ln_1

    with model.trace(PROMPT):
        norm.input[:] = 0
        logits = model.lm_head.output[0, -1].save()
    local = logits

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        norm.input[:] = 0
        logits = model.lm_head.output[0, -1].save()

    saved = run_sandboxed(model, capture.blob)
    assert torch.allclose(saved["logits"], local)


def test_an_in_place_edit_in_one_of_two_invokes(frozen):
    # With two invokes the block reads its own rows of the batch, a view.
    model = frozen
    norm = model.transformer.h[5].ln_1

    with model.trace() as tracer:
        with tracer.invoke(PROMPT):
            clean = model.lm_head.output[0, -1].save()
        with tracer.invoke(PROMPT):
            norm.input[:] = 0
            logits = model.lm_head.output[0, -1].save()
    local_clean, local = clean, logits

    capture = Capture()
    with model.trace(backend=capture) as tracer:
        with tracer.invoke(PROMPT):
            clean = model.lm_head.output[0, -1].save()
        with tracer.invoke(PROMPT):
            norm.input[:] = 0
            logits = model.lm_head.output[0, -1].save()

    saved = run_sandboxed(model, capture.blob)
    assert torch.allclose(saved["clean"], local_clean)
    assert torch.allclose(saved["logits"], local)
    assert not torch.allclose(local, local_clean)


def test_reading_a_layer_input_keeps_the_kv_cache(frozen):
    # No edit at all: one read of a block's input on the first step of a cached
    # generate, then the logits of every step.
    model = frozen
    block = model.transformer.h[5]

    with model.generate(PROMPT, **GENERATE) as tracer:
        hidden = block.input.save()
        steps = nnsight.save([])
        for step in tracer.iter[:3]:
            steps.append(model.lm_head.output[0, -1].clone())
    local = [logits.detach().clone() for logits in steps]

    capture = Capture()
    with model.generate(PROMPT, backend=capture, **GENERATE) as tracer:
        hidden = block.input.save()
        steps = nnsight.save([])
        for step in tracer.iter[:3]:
            steps.append(model.lm_head.output[0, -1].clone())

    saved = run_sandboxed(model, capture.blob)
    assert len(saved["steps"]) == len(local) == 3
    for step, (expected, actual) in enumerate(zip(local, saved["steps"])):
        assert torch.allclose(actual, expected), f"step {step} diverged from the trusted run"


def test_write_back_edits_tensors_and_replaces_nothing():
    # No model: the shape of a layer's `.input`, with a tensor the block edited,
    # a tensor that cannot be written in place, and an object with an identity.
    hidden = torch.zeros(3, 4)
    mask = torch.zeros(1, 4).expand(3, 4)
    cache = object()
    served = ((hidden,), {"attention_mask": mask, "cache": cache})
    edited = ((torch.ones(3, 4),), {"attention_mask": mask.clone(), "cache": object()})

    write_back(served, edited)

    assert served[0][0] is hidden
    assert torch.equal(hidden, torch.ones(3, 4))
    assert served[1]["attention_mask"] is mask
    assert served[1]["cache"] is cache


def tensors_of(value):
    found = []
    apply(value, found.append, torch.Tensor)
    return found


def layer_input():
    return ((torch.zeros(2, 3),), {"attention_mask": torch.ones(2, 3), "cache": object()})


EDITS = {
    "in place": (lambda value: value[0][0].add_(1), True),
    # Does not move the tensor's version counter.
    "through .data": (lambda value: value[0][0].data.fill_(1), True),
    "none": (lambda value: None, False),
    "an element replaced": (
        lambda value: value[1].update(attention_mask=torch.zeros(2, 3)),
        True,
    ),
    "an element replaced by an equal one": (
        lambda value: value[1].update(attention_mask=torch.ones(2, 3)),
        False,
    ),
    "a shape changed": (
        lambda value: value[1].update(attention_mask=torch.ones(1, 3)),
        False,
    ),
    "a key added": (lambda value: value[1].update(extra=torch.ones(1)), False),
    "a non-tensor rebound": (lambda value: value[1].update(cache=None), False),
}


@pytest.mark.parametrize("name", EDITS)
def test_the_runner_sends_a_copy_back_exactly_when_write_back_would_write_it(name):
    edit, expected = EDITS[name]
    served = layer_input()
    received = copy.deepcopy(served)  # the runner's copy, as it arrives
    before = snapshot(received)
    edit(received)
    sent = edited(received, before)

    untouched = copy.deepcopy(served)
    write_back(served, received)
    written = not all(
        torch.equal(now, then)
        for now, then in zip(tensors_of(served), tensors_of(untouched))
    )
    assert sent == written == expected


def test_only_an_edited_read_is_sent_back(frozen, monkeypatch):
    model = frozen
    norm = model.transformer.h[5].ln_1
    written = []
    real = driver.write_back
    monkeypatch.setattr(
        driver, "write_back", lambda served, copy: (written.append(copy), real(served, copy))
    )

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        hidden = norm.input.save()
        logits = model.lm_head.output[0, -1].save()
    run_sandboxed(model, capture.blob)
    assert written == []

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        norm.input[:] = 0
        logits = model.lm_head.output[0, -1].save()
    run_sandboxed(model, capture.blob)
    assert len(written) == 1


def test_an_edit_through_data_reaches_the_host(frozen):
    model = frozen
    norm = model.transformer.h[5].ln_1

    with model.trace(PROMPT):
        norm.input.data[:] = 0
        logits = model.lm_head.output[0, -1].save()
    local = logits

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        norm.input.data[:] = 0
        logits = model.lm_head.output[0, -1].save()

    saved = run_sandboxed(model, capture.blob)
    assert torch.allclose(saved["logits"], local)
