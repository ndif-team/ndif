"""A sandboxed block edits the host's own values, not copies of them.

A value a block reads crosses the socket as a copy, and the runner sends the copy
back if the block edited it, so in-place edits reach the forward. The host used
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

The runner sends a copy back only when the block edited it, which it tells by
running ``write_back`` against a copy it kept. And what crosses on a read is a
compact copy (``controls.compact``), so one invoke's read carries its own rows
and not the whole batch.

Same harness as ``test_sandbox_iter_pins``, whose helpers this imports: no server
and no Ray, a real gpt2 on CPU, a runner spawned over a Unix socket and a
``SandboxDriver`` pumping it. The trusted run is the ground truth.
"""

from __future__ import annotations

import io

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from ndif.services.ray.sandbox import driver, host
from ndif.services.ray.sandbox.controls import compact, write_back
from ndif.services.ray.sandbox.messages import Payload

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

    assert write_back(served, edited)

    assert served[0][0] is hidden
    assert torch.equal(hidden, torch.ones(3, 4))
    assert served[1]["attention_mask"] is mask
    assert served[1]["cache"] is cache
    # Nothing differs now, which is how the runner tells a read it need not send.
    assert not write_back(served, edited)


def test_compact_sends_a_view_as_its_own_rows():
    batch = torch.arange(24.0).reshape(4, 6)
    rows = batch[1:2]
    fresh = torch.ones(3)
    sent = compact((rows, fresh))
    assert torch.equal(sent[0], rows)
    assert sent[0].untyped_storage().nbytes() == rows.numel() * rows.element_size()
    assert sent[1] is fresh


def test_compact_leaves_what_it_cannot_shrink_or_must_not_split():
    batch = torch.zeros(4, 3)
    for value in (
        torch.zeros(2, 3),  # owns its storage
        batch[0:1, 0:1].expand(1, 3),  # a copy could be edited, and this cannot
        (batch[0:1], batch[0:1]),  # copied apart, an edit of one would miss the other
        # The same, where the other tensor is a parameter.
        (weight := torch.nn.Parameter(torch.zeros(4, 3)), weight.data[0:1]),
    ):
        assert compact(value) is value


def run_measured(model, blob):
    """Run ``blob`` in a runner; return its saved values and the size of every
    frame the host sent."""
    sandbox = host.spawn(model_key=model.to_model_key())
    sizes = []
    try:
        connection = sandbox.connection()
        send_raw = connection.send_raw
        connection.send_raw = lambda data: (sizes.append(len(data)), send_raw(data))
        connection.send(Payload(blob, False, "torch.float32", None, {}))
        data, _ = driver.SandboxDriver(model, torch.float32).pump(connection)
        return torch.load(io.BytesIO(data), weights_only=False), sizes
    finally:
        sandbox.stop()


def test_a_read_carries_only_its_invokes_rows(frozen):
    model = frozen
    prompt = " ".join([PROMPT] * 8)
    mlp = model.transformer.h[0].mlp

    capture = Capture()
    with model.trace(backend=capture) as tracer:
        with tracer.invoke(prompt):
            first = mlp.output.save()
        with tracer.invoke(prompt):
            second = mlp.output.save()

    saved, sizes = run_measured(model, capture.blob)
    rows = saved["first"].numel() * saved["first"].element_size()
    # The first frame is the request and the last is `Done`, which carries the
    # model's whole output; between them are the two reads.
    assert len(sizes) == 4
    assert max(sizes[1:-1]) < 1.5 * rows


def test_only_an_edited_read_is_sent_back(frozen, monkeypatch):
    model = frozen
    norm = model.transformer.h[5].ln_1
    written = []
    real = driver.write_back
    monkeypatch.setattr(
        driver, "write_back", lambda served, copy: (written.append(copy), real(served, copy))
    )

    # Two reads, one of them edited.
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
