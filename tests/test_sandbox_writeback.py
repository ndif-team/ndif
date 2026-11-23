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

Writing into the originals reaches one tensor it must not: a weight. A value a
block reads can hold the module's ``weight`` itself, and an edit of it written
back stayed in the model after the request. ``write_back`` refuses a tensor
stored where a parameter or a buffer is, and the request fails with
``WeightEditError``.

Same harness as ``test_sandbox_iter_pins``, whose helpers this imports: no server
and no Ray, a real gpt2 on CPU, a runner spawned over a Unix socket and a
``SandboxDriver`` pumping it. The trusted run is the ground truth.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from ndif.common.errors import WeightEditError
from ndif.services.ray.sandbox.controls import kept_by, write_back

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


def test_an_in_place_edit_of_a_weight_is_refused(frozen):
    # The third argument of the norm's F.layer_norm call is its `weight`.
    model = frozen
    weight = model._module.transformer.h[0].ln_1.weight
    before = weight.detach().clone()

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        model.transformer.h[0].ln_1.source.F_layer_norm_0.inputs[0][2][:] = 0
        logits = model.lm_head.output[0, -1].save()

    try:
        with pytest.raises(WeightEditError):
            run_sandboxed(model, capture.blob)
        assert torch.equal(weight, before)
    finally:
        weight.copy_(before)  # the model is shared with the other tests


def test_write_back_refuses_the_models_own_storage():
    # No model: a module with a weight and a buffer, and values that hold them.
    module = torch.nn.BatchNorm1d(4).requires_grad_(False)
    kept = kept_by(module)
    hidden = torch.zeros(3, 4)

    # A value with a weight in it can be read, and its other tensors edited.
    write_back((hidden, module.weight), (torch.ones(3, 4), module.weight.clone()), kept)
    assert torch.equal(hidden, torch.ones(3, 4))

    # An edit of the weight, of a view of it, or of a buffer is refused.
    for served in (module.weight, module.weight[2:], module.running_mean):
        before = served.clone()
        with pytest.raises(WeightEditError):
            write_back({"x": (served,)}, {"x": (served + 1,)}, kept)
        assert torch.equal(served, before)


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
