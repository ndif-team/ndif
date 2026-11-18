"""A sandboxed block on a model split over several cards keeps the KV cache in place.

Every value a block reads is written back to the host when the block moves on,
so in-place edits reach the forward. The host rebuilt everything the runner sent
on ``model.device``, which for a model a device map spreads over several cards is
the first card. A read of ``model.output`` carries the whole ``past_key_values``,
and accelerate's hooks never move a cache, so it stayed on the first card. The
next forward's first layer on another card then failed in the cache update:
``RuntimeError: Expected all tensors to be on the same device, but got tensors is
on cuda:1, different from other tensors on cuda:0 (when checking argument in
method wrapper_CUDA_cat)``. A cache passed from one trace into the next failed
the same way. The trusted path was unaffected.

The fix is the host's ``map_location`` (``driver.landing``): a tensor sent from
one of the model's cards is rebuilt on that card.

Same harness as ``test_sandbox_iter_pins``, whose helpers this imports: no server
and no Ray, a runner spawned over a Unix socket and a ``SandboxDriver`` pumping
it. Here gpt2 is split over two cards with ``redispatch``, the way the WARM to
HOT path places a multi-card replica, and the trusted run is the ground truth.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")
pytest.importorskip(
    "boto3", reason="the server package's imports need the server's dependencies"
)
if torch.cuda.device_count() < 2:
    pytest.skip("needs two CUDA devices", allow_module_level=True)

from ndif.services.ray.deployments.modeling.util import redispatch

from test_sandbox_iter_pins import PROMPT, REPO, Capture, run_sandboxed

GENERATE = dict(max_new_tokens=3, min_new_tokens=3, do_sample=False, use_cache=True)


@pytest.fixture(scope="module")
def model():
    """gpt2 with blocks 6 to 11 on cuda:1 and everything else on cuda:0."""
    from nnsight import TransformersModel

    try:
        model = TransformersModel(REPO, task="text-generation", device="cpu", dispatch=True)
    except Exception as error:  # no cached weights and no network, most likely
        pytest.skip(f"could not load {REPO}: {error}")
    layers = len(model.transformer.h)
    device_map = {
        "transformer.wte": 0,
        "transformer.wpe": 0,
        "transformer.ln_f": 0,
        "lm_head": 0,
        **{f"transformer.h.{index}": int(index >= layers // 2) for index in range(layers)},
    }
    redispatch(model._module, device_map)
    assert model.devices == {torch.device("cuda", 0), torch.device("cuda", 1)}
    return model


def test_generate_reading_model_output_every_step(model):
    # The reported block: read the logits off `model.output` at every step of a
    # cached generate.
    with model.generate(PROMPT, **GENERATE) as tracer:
        picks = nnsight.save([])
        for step in tracer.iter[:3]:
            picks.append(model.output.logits[0, -1].argmax().cpu())
    local = [int(pick) for pick in picks]

    capture = Capture()
    with model.generate(PROMPT, backend=capture, **GENERATE) as tracer:
        picks = nnsight.save([])
        for step in tracer.iter[:3]:
            picks.append(model.output.logits[0, -1].argmax().cpu())

    saved = run_sandboxed(model, capture.blob)
    assert [int(pick) for pick in saved["picks"]] == local


def test_a_cache_passed_into_the_next_trace(model):
    # The reporter's other approach: a prefix trace, then a trace that continues
    # from its cache.
    ids = model.tokenizer(PROMPT, return_tensors="pt").input_ids

    with model.session():
        with model.trace(input_ids=ids[:, :-1], use_cache=True):
            cache = model.output.past_key_values
        with model.trace(input_ids=ids[:, -1:], past_key_values=cache, use_cache=True):
            top = model.output.logits[0, -1].cpu().save()
    local = top

    capture = Capture()
    with model.session(backend=capture):
        with model.trace(input_ids=ids[:, :-1], use_cache=True):
            cache = model.output.past_key_values
        with model.trace(input_ids=ids[:, -1:], past_key_values=cache, use_cache=True):
            top = model.output.logits[0, -1].cpu().save()

    saved = run_sandboxed(model, capture.blob)
    assert torch.allclose(saved["top"], local)
