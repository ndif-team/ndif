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

Writing into the originals reaches one tensor it must not: a weight. A value a
block reads can hold the module's ``weight`` itself, and an edit of it written
back stayed in the model after the request. ``write_back`` refuses a tensor
stored where a parameter or a buffer is, and the request fails with
``WeightEditError``.

The runner sends a copy back only when the block edited it, which it tells by
running ``write_back`` against a copy it kept. And what crosses on a read is a
compact copy (``controls.compact``), so one invoke's read carries its own rows
and not the whole batch.

A KV cache stays on the host. It is in the keyword arguments of every decoder
layer and in the model's output, so it was most of the bytes of a read of
``.input``, and a block that assigned ``.input`` or ``.inputs`` sent a copy of it
back, which replaced the one the forward held: later steps of a cached
``generate`` diverged from the trusted run. The host now sends a stand-in in a
cache's place (``controls.withhold``) and puts its own cache back wherever the
stand-in returns in an assigned value (``controls.own``).

Same harness as ``test_sandbox_iter_pins``, whose helpers this imports: no server
and no Ray, a real gpt2 on CPU, a runner spawned over a Unix socket and a
``SandboxDriver`` pumping it. The trusted run is the ground truth.
"""

from __future__ import annotations

import io

import pytest
from nnsight.util import apply
from transformers.cache_utils import Cache

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from ndif.common.errors import WeightEditError
from ndif.services.ray.sandbox import driver, host
from ndif.services.ray.sandbox.controls import (
    compact,
    kept_by,
    own,
    withhold,
    write_back,
)
from ndif.services.ray.sandbox.messages import OnHost, Payload

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
        driver,
        "write_back",
        lambda served, copy, kept: (written.append(copy), real(served, copy, kept)),
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


def test_withhold_and_own_keep_a_cache_on_the_host():
    from transformers import DynamicCache

    cache, other, hidden = DynamicCache(), DynamicCache(), torch.zeros(2, 3)
    held = []

    sent = withhold(((hidden,), {"past_key_values": cache, "step": 0}), held)
    assert sent == ((hidden,), {"past_key_values": OnHost(0), "step": 0})
    assert sent[0][0] is hidden
    # The same cache met again keeps its number; another one gets the next.
    assert withhold([cache, other], held) == [OnHost(0), OnHost(1)]
    assert held[0] is cache and held[1] is other

    (new,), kwargs = own(((hidden * 2,), {"past_key_values": OnHost(0)}), held)
    assert kwargs["past_key_values"] is cache

    for key in (2, -1, True, "0"):
        with pytest.raises(LookupError):
            own((OnHost(key),), held)
    # A block that reads into the stand-in is told why it cannot.
    with pytest.raises(AttributeError, match="stays on the host"):
        OnHost(0).layers


def test_a_read_of_a_blocks_inputs_leaves_the_kv_cache_on_the_host(frozen):
    model = frozen
    block = model.transformer.h[5]

    with model.trace(PROMPT):
        local = block.input.save()
    hidden_bytes = local.numel() * local.element_size()

    # `.input` and `.inputs` are one read of the call's ``(args, kwargs)``.
    for read in ("input", "inputs"):
        capture = Capture()
        with model.trace(PROMPT, backend=capture):
            if read == "input":
                hidden = block.input.save()
            else:
                hidden = block.inputs[0][0].save()
        saved, sizes = run_measured(model, capture.blob)
        # The request, the one read, and `Done`.
        assert len(sizes) == 3
        assert torch.equal(saved["hidden"], local)
        assert sizes[1] < 1.5 * hidden_bytes


def test_an_in_place_edit_of_a_blocks_input_reaches_the_forward(frozen):
    # The block's call has other arguments, so the read is a part of it, and the
    # edit of that part has to land in the host's whole.
    model = frozen
    block = model.transformer.h[5]

    with model.trace(PROMPT):
        block.input[:, -1] = 0
        logits = model.lm_head.output[0, -1].save()
    local = logits.detach().clone()
    with model.trace(PROMPT):
        untouched = model.lm_head.output[0, -1].save()
    assert not torch.allclose(local, untouched)

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        block.input[:, -1] = 0
        logits = model.lm_head.output[0, -1].save()

    saved = run_sandboxed(model, capture.blob)
    assert torch.allclose(saved["logits"], local)


def test_assigning_input_keeps_the_kv_cache(frozen):
    # The assignment used to bring the whole call back, and the cache in it
    # replaced the one the forward held: the steps after it lost their history.
    model = frozen
    block = model.transformer.h[5]

    with model.generate(PROMPT, **GENERATE) as tracer:
        block.input = block.input * 0.5
        steps = nnsight.save([])
        for step in tracer.iter[:3]:
            steps.append(model.lm_head.output[0, -1].clone())
    local = [logits.detach().clone() for logits in steps]

    capture = Capture()
    with model.generate(PROMPT, backend=capture, **GENERATE) as tracer:
        block.input = block.input * 0.5
        steps = nnsight.save([])
        for step in tracer.iter[:3]:
            steps.append(model.lm_head.output[0, -1].clone())

    saved = run_sandboxed(model, capture.blob)
    assert len(saved["steps"]) == len(local) == 3
    for step, (expected, actual) in enumerate(zip(local, saved["steps"])):
        assert torch.allclose(actual, expected), f"step {step} diverged from the trusted run"


def test_assigning_a_source_operations_input_matches_the_trusted_run(frozen):
    model = frozen
    operation = model.transformer.h[5].mlp.source.self_act_0

    with model.trace(PROMPT):
        operation.input = operation.input * 0.5
        logits = model.lm_head.output[0, -1].save()
    local = logits.detach().clone()

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        operation.input = operation.input * 0.5
        logits = model.lm_head.output[0, -1].save()

    saved = run_sandboxed(model, capture.blob)
    assert torch.allclose(saved["logits"], local, atol=1e-5)


def test_assigning_inputs_keeps_the_kv_cache(frozen):
    # The whole call, assigned with a new first argument: the other arguments,
    # the cache among them, are the ones the block read.
    model = frozen
    block = model.transformer.h[5]
    with model.generate(PROMPT, **GENERATE) as tracer:
        args, kwargs = block.inputs
        block.inputs = ((args[0] * 0.5, *args[1:]), kwargs)
        steps = nnsight.save([])
        for step in tracer.iter[:3]:
            steps.append(model.lm_head.output[0, -1].clone())
    local = [logits.detach().clone() for logits in steps]

    capture = Capture()
    with model.generate(PROMPT, backend=capture, **GENERATE) as tracer:
        args, kwargs = block.inputs
        block.inputs = ((args[0] * 0.5, *args[1:]), kwargs)
        steps = nnsight.save([])
        for step in tracer.iter[:3]:
            steps.append(model.lm_head.output[0, -1].clone())

    saved = run_sandboxed(model, capture.blob)
    assert len(saved["steps"]) == len(local) == 3
    for step, (expected, actual) in enumerate(zip(local, saved["steps"])):
        assert torch.allclose(actual, expected), f"step {step} diverged from the trusted run"


def test_saved_inputs_hold_none_where_the_kv_cache_was(frozen):
    model = frozen
    block = model.transformer.h[5]

    capture = Capture()
    with model.generate(PROMPT, backend=capture, **GENERATE):
        call = block.inputs.save()
    args, kwargs = run_sandboxed(model, capture.blob)["call"]

    with model.generate(PROMPT, **GENERATE):
        local = block.inputs.save()
    assert torch.equal(args[0], local[0][0])
    there, here = [], []
    apply(local, there.append, Cache)
    apply((args, kwargs), here.append, (Cache, OnHost))
    assert there and not here
