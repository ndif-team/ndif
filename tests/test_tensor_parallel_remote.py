"""A tensor-parallel replica, end to end, over ``remote=True``.

The rest of this suite runs against gpt2 on a single GPU. This one covers the
other deployment shape: a model split across GPUs by
``ndif.services.ray.tp.model.TPModelActor``, where the actor process is rank 0
and spawns the other ranks, every rank runs the user's block, and sharded
activations are gathered so the client sees whole tensors.

Nothing in the traces below mentions sharding — that is the point. They are
ordinary remote traces, and they assert the two things a broken shard-and-gather
would get wrong: the **width** of a value, and whether the answer matches what
the same model produces unsharded.

Skipped unless a tensor-parallel replica is actually deployed, since that needs
several GPUs and an explicit deploy. To set one up::

    cat > tp.yaml <<'YAML'
    models:
      - checkpoint: meta-llama/Llama-3.2-3B
        replicas: 1
        pinned: true
        trusted: true
        dtype: bfloat16
        actor_class: ndif.services.ray.tp.model.TPModelActor
        padding_factor: 35.6   # forces a 4-GPU allocation; see the actor's docs
    YAML
    ndif deploy -f tp.yaml
"""

import pytest
import torch

from conftest import HOST, requires_server

# Divides cleanly by 4 on heads, kv-heads and intermediate size.
TP_REPO = "meta-llama/Llama-3.2-3B"
TP_PROMPT = "The Eiffel Tower is in the city of"
TP_LAYER = 5
TP_HIDDEN = 3072
TP_INTERMEDIATE = 8192
TP_VOCAB = 128256


def _tp_model_running() -> bool:
    """Whether a replica for the tensor-parallel model is deployed and HOT."""
    try:
        import nnsight
        from nnsight import ndif

        nnsight.CONFIG.API.HOST = HOST
        return bool(ndif.is_model_running(TP_REPO))
    except Exception:
        return False


requires_tp_deployment = pytest.mark.skipif(
    not _tp_model_running(),
    reason=(
        f"no tensor-parallel replica of {TP_REPO} deployed "
        "(see this module's docstring to deploy one)"
    ),
)

pytestmark = [requires_server, requires_tp_deployment]


@pytest.fixture(scope="module")
def tp_model():
    """An undispatched client model. The server holds the sharded weights; the
    client only needs the architecture to build the request."""
    from nnsight.modeling.transformers import TransformersModel

    return TransformersModel(TP_REPO, task="text-generation")


class TestShardedWidths:
    """A value comes back whole — neither a fraction of the real tensor nor a
    multiple of it."""

    def test_column_parallel_output_is_gathered(self, tp_model):
        # Each rank computes intermediate/tp_size of these features; the user
        # asked for the layer, so all of them come back.
        with tp_model.trace(TP_PROMPT, remote=True):
            gate = tp_model.model.layers[TP_LAYER].mlp.gate_proj.output.save()
        assert gate.shape[-1] == TP_INTERMEDIATE

    def test_row_parallel_input_is_gathered(self, tp_model):
        # A row-parallel layer takes its input already split across ranks.
        with tp_model.trace(TP_PROMPT, remote=True):
            down_in = tp_model.model.layers[TP_LAYER].mlp.down_proj.input.save()
        assert down_in.shape[-1] == TP_INTERMEDIATE

    def test_a_replicated_value_is_untouched(self, tp_model):
        # A row-parallel layer all-reduces its output, so a decoder layer is
        # already whole and nothing should gather it a second time.
        with tp_model.trace(TP_PROMPT, remote=True):
            layer_out = tp_model.model.layers[TP_LAYER].output[0].save()
        assert layer_out.shape[-1] == TP_HIDDEN

    def test_logits_are_the_vocabulary_not_a_multiple_of_it(self, tp_model):
        # The tied-embedding failure: a head whose weight was never sharded gets
        # all-gathered anyway and returns tp_size * vocab. Silent — the argmax
        # still lands in the first copy — so the width is what catches it.
        with tp_model.trace(TP_PROMPT, remote=True):
            logits = tp_model.lm_head.output.save()
        assert logits.shape[-1] == TP_VOCAB


class TestShardedIntervention:
    """Editing a sharded value reaches the model."""

    def test_zeroing_a_column_parallel_output_changes_the_prediction(self, tp_model):
        with tp_model.trace(TP_PROMPT, remote=True):
            baseline = tp_model.lm_head.output.save()

        with tp_model.trace(TP_PROMPT, remote=True):
            for layer in range(TP_LAYER, TP_LAYER + 8):
                tp_model.model.layers[layer].mlp.gate_proj.output[:] = 0
            wrecked = tp_model.lm_head.output.save()

        assert wrecked.shape == baseline.shape
        assert not torch.equal(wrecked, baseline)

    def test_a_partial_edit_straddling_rank_boundaries_applies(self, tp_model):
        # 3000 of 8192 crosses a boundary at tp=4 (2048 each): ranks 0 and 1 are
        # hit, 2 and 3 are not. Only correct if the gather assembled in rank
        # order and the re-split put the edit back where it came from.
        with tp_model.trace(TP_PROMPT, remote=True):
            baseline = tp_model.lm_head.output.save()

        with tp_model.trace(TP_PROMPT, remote=True):
            tp_model.model.layers[TP_LAYER].mlp.gate_proj.output[..., :3000] = 0
            edited = tp_model.lm_head.output.save()

        assert not torch.equal(edited, baseline)


class TestShardedDeterminism:
    """Every rank runs the block, so the replica has to answer consistently."""

    def test_the_same_request_twice_gives_the_same_answer(self, tp_model):
        with tp_model.trace(TP_PROMPT, remote=True):
            first = tp_model.lm_head.output.save()
        with tp_model.trace(TP_PROMPT, remote=True):
            second = tp_model.lm_head.output.save()

        assert torch.equal(first, second)

    def test_greedy_generation_is_reproducible(self, tp_model):
        # Sampling that diverges across ranks is a correctness bug, not just an
        # inconsistency: the ranks would all-reduce activations computed from
        # different tokens. Greedy removes the variable; the actor seeds every
        # rank identically for the sampled case.
        runs = []
        for _ in range(2):
            with tp_model.generate(
                TP_PROMPT, max_new_tokens=5, do_sample=False, remote=True
            ) as tracer:
                out = tracer.result.save()
            runs.append(out)

        assert torch.equal(runs[0], runs[1])
