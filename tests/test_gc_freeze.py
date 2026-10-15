"""Loaded-model objects are frozen out of the per-request ``gc.collect``.

``cleanup()`` runs a full ``gc.collect`` after every request. Before the freeze
it re-scanned every object of the loaded model each time. ``freeze_loaded_objects``
collects what is already garbage, then moves the survivors to the permanent
generation, which ``gc.collect`` and ``gc.get_objects`` skip.
"""

from __future__ import annotations

import gc
import weakref

import pytest
import torch

from ndif.services.ray.deployments.modeling.base import freeze_loaded_objects


@pytest.fixture(autouse=True)
def unfreeze():
    yield
    gc.unfreeze()


class Node:
    pass


def test_survivors_frozen_and_prior_garbage_collected():
    model = torch.nn.Sequential(torch.nn.Linear(4, 4), torch.nn.ReLU())

    cycle = Node()
    cycle.self = cycle
    dead = weakref.ref(cycle)
    del cycle

    freeze_loaded_objects()

    assert dead() is None
    tracked = {id(o) for o in gc.get_objects()}
    assert id(model) not in tracked
    assert id(model[0]) not in tracked
    assert id(model[0]._parameters) not in tracked


def test_objects_created_after_freeze_are_still_collected():
    freeze_loaded_objects()

    cycle = Node()
    cycle.self = cycle
    dead = weakref.ref(cycle)
    del cycle
    gc.collect()

    assert dead() is None


def _tiny_model(tmp_path):
    """A 2-layer Qwen2 built in memory, plus two LoRA adapters on disk that target
    different projections. No download: the tokenizer is built here too."""
    from peft import LoraConfig, get_peft_model
    from tokenizers import Tokenizer, models, pre_tokenizers
    from transformers import PreTrainedTokenizerFast, Qwen2Config, Qwen2ForCausalLM
    from nnsight.modeling.transformers import TransformersModel

    config = Qwen2Config(
        vocab_size=64, hidden_size=32, intermediate_size=64, num_hidden_layers=2,
        num_attention_heads=4, num_key_value_heads=2,
    )
    adapters = {}
    for name, seed, targets in (
        ("adapter_qv", 1, ["q_proj", "v_proj"]),
        ("adapter_ko", 2, ["k_proj", "o_proj"]),
    ):
        torch.manual_seed(seed)
        lora = LoraConfig(r=4, target_modules=targets, init_lora_weights=False)
        get_peft_model(Qwen2ForCausalLM(config), lora).save_pretrained(tmp_path / name)
        adapters[name] = str(tmp_path / name)

    vocab = {"[UNK]": 0} | {f"w{i}": i for i in range(1, 64)}
    backend = Tokenizer(models.WordLevel(vocab, unk_token="[UNK]"))
    backend.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer = PreTrainedTokenizerFast(
        tokenizer_object=backend, unk_token="[UNK]", pad_token="[UNK]"
    )
    torch.manual_seed(0)
    return TransformersModel(Qwen2ForCausalLM(config), tokenizer=tokenizer), adapters


def test_peft_swaps_release_dropped_envoys_and_adapters(tmp_path):
    """A PEFT swap rebuilds the Envoy tree and wraps/unwraps the frozen base
    modules, so it is where frozen objects become garbage after the freeze.
    They hold no reference cycle, so refcounting frees them; a cycle would keep
    them (and anything they reference) alive for the replica's lifetime."""
    model, adapters = _tiny_model(tmp_path)
    freeze_loaded_objects()
    load_time_envoys = [weakref.ref(child) for child in model._children]
    assert load_time_envoys
    tracked = {id(o) for o in gc.get_objects()}
    assert not any(id(ref()) in tracked for ref in load_time_envoys)  # frozen

    def request(env):
        model._remoteable_set_env(env)
        with model.trace(input_ids=torch.tensor([[1, 2, 3, 4]])):
            out = model.output.save()
        return weakref.ref(out.logits)

    def cleanup():
        model.interleaver.cancel()
        gc.collect()

    previous_lora = []
    for adapter in [*adapters.values(), None] * 2:
        saved = request({"peft": adapter} if adapter else None)
        cleanup()

        assert saved() is None
        assert all(ref() is None for ref in previous_lora)
        # A swap keeps standalone children (the generator) and drops the rest.
        # `is`, not `in`: Envoy defines __len__/__eq__ over its module.
        survivors = [ref() for ref in load_time_envoys if ref() is not None]
        assert all(any(s is c for c in model._children) for s in survivors)
        assert len(survivors) < len(load_time_envoys)
        previous_lora = [
            weakref.ref(p) for n, p in model._module.named_parameters() if "lora_" in n
        ]
        assert bool(previous_lora) == (adapter is not None)
