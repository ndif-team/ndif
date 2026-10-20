"""``redispatch``, the WARM→HOT move, keeps nnsight's instrumentation (#339).

A multi-GPU load goes through accelerate, which puts an alignment wrapper in each
placed module's instance ``forward`` and the real forward on ``_old_forward``;
nnsight then installs its controller into that same slot. Restoring from cache
re-dispatches, and a bare strip-and-dispatch writes ``_old_forward`` back over the
controller: the model runs uninstrumented and every trace raises
``OutOfOrderError`` until the replica restarts.

Needs one CUDA device, not several: a map spanning several devices is what makes
accelerate install its hooks, so they are forced here instead, on the initial
load and on every re-dispatch, in the order a multi-GPU load produces them
(accelerate first, nnsight second). No server.
"""

from __future__ import annotations

import functools

import pytest
import torch

pytest.importorskip(
    "boto3", reason="the server package's imports need the server's dependencies"
)
if not torch.cuda.is_available():
    pytest.skip("needs a CUDA device", allow_module_level=True)

import accelerate
from nnsight.modeling.transformers import TransformersModel
from transformers import AutoModelForCausalLM
from transformers.integrations.accelerate import _get_device_map

from ndif.services.ray.deployments.modeling.util import redispatch

REPO = "gpt2"


@pytest.fixture(scope="module")
def model():
    """gpt2 hooked the way a multi-GPU ``from_pretrained`` hooks it, then wrapped.

    ``skip_keys`` is what transformers passes; the per-block map mirrors what
    ``device_map="balanced"`` resolves to.
    """
    raw = AutoModelForCausalLM.from_pretrained(REPO, dtype=torch.float32)
    blocks = [name for name, _ in raw.named_modules() if name.startswith("transformer.h.") and name.count(".") == 2]
    device_map = {
        "transformer.wte": 0,
        "transformer.wpe": 0,
        "transformer.ln_f": 0,
        "lm_head": 0,
        **{block: 0 for block in blocks},
    }
    accelerate.dispatch_model(raw, device_map=device_map, force_hooks=True, skip_keys=raw._skip_keys_device_placement)
    assert "_old_forward" in raw.transformer.h[0].__dict__, "accelerate did not hook the blocks"
    return TransformersModel(raw, REPO)


def run(model):
    with model.trace("The quick brown fox"):
        layer0 = model.transformer.h[0].output.save()
        logits = model.output.logits.save()
    return layer0[0], logits


def cache_cycle(model, monkeypatch):
    """``to_cache`` then ``from_cache``, as the actor does them, minus the actor.

    ``redispatch`` imports ``dispatch_model`` from ``accelerate`` at call time, so
    forcing the hooks there reaches it.
    """
    module = model._module
    module.to("cpu")
    torch.cuda.synchronize()
    device_map = _get_device_map(module, "balanced", {0: 40 * 2**30}, None)
    monkeypatch.setattr(accelerate, "dispatch_model", functools.partial(accelerate.dispatch_model, force_hooks=True))
    redispatch(module, device_map)
    torch.cuda.synchronize()
    assert "_hf_hook" in module.transformer.h[0].__dict__, "the re-dispatch did not hook the blocks"


def test_trace_survives_cache_cycles(model, monkeypatch):
    layer0, logits = run(model)
    forwards = {id(m): id(m.__dict__["forward"]) for m in model._module.modules() if "forward" in m.__dict__}

    for _ in range(2):  # a replica can be cycled many times; the second cycle starts from our own state
        cache_cycle(model, monkeypatch)
        assert {str(p.device) for p in model._module.parameters()} == {"cuda:0"}
        after = {id(m): id(m.__dict__["forward"]) for m in model._module.modules() if "forward" in m.__dict__}
        assert after == forwards, "a module's forward changed across the re-dispatch"
        layer0_after, logits_after = run(model)  # raised OutOfOrderError before the fix
        torch.testing.assert_close(layer0_after, layer0)
        torch.testing.assert_close(logits_after, logits)
