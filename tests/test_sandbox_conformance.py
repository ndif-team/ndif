"""Trusted and untrusted requests must compute the same thing.

`trusted` decides *where* a request's Python runs — in the model actor next to
the weights, or in a sandbox runner process driven over a socket — and nothing
else. So the same script must return the same bytes either way. That invariant is
the whole reason the sandbox is shaped as it is, and it is the one thing about it
no other test checks.

It was false, twice over, and neither failure raised anything:

* the runner had no autocast region, so a tensor the *block* made came back
  float32 untrusted and bfloat16 in-process;
* and the host had none around the forward it drives on the runner's behalf, so
  the *model's own* arithmetic ran uncast — identical token ids and embeddings,
  diverging inside the first transformer block, 6.5e-3 relative in the logits.

Both were found by measuring by hand. What replaced the measurement was a set of
`inspect.getsource` substring assertions, which pass whether or not the numbers
agree. This is the measurement, kept.

Needs a running server with the suite's model deployed, like the rest of the
remote tests. nnsight has no `trusted` on its request envelope — the server reads
it from the request body, where an API key would normally stamp it — so this
injects it, which is what docs/developing/testing.md describes as the way to
reach the sandbox path with auth off.
"""

import json
from contextlib import contextmanager

import pytest
import torch

from conftest import PROMPT, REPO, requires_server

pytestmark = requires_server


@contextmanager
def _trusted(value: bool):
    """Run the requests in this block with ``trusted`` set to ``value``."""
    from nnsight.schema.request import RequestModel

    original = RequestModel.metadata

    def metadata(self):
        body = json.loads(original(self))
        body["trusted"] = value
        return json.dumps(body)

    RequestModel.metadata = metadata
    try:
        yield
    finally:
        RequestModel.metadata = original


@pytest.fixture(scope="module")
def model():
    from nnsight.modeling.transformers import TransformersModel

    return TransformersModel(REPO, task="text-generation")


def _run(model):
    """One trace, returning everything worth comparing.

    ``made`` is the load-bearing one: a tensor the *block* computes rather than
    one the model returns. That is what the autocast region governs, and the only
    value whose dtype ever differed.
    """
    with model.trace(PROMPT, remote=True):
        hidden = model.transformer.h[5].output[0].save()
        logits = model.lm_head.output.save()
        made = (hidden.float() @ hidden.float().transpose(-1, -2)).save()
    return {"hidden": hidden, "logits": logits, "made": made}


@pytest.fixture(scope="module")
def runs(model):
    with _trusted(True):
        first = _run(model)
        control = _run(model)
    with _trusted(False):
        second = _run(model)
    return first, control, second


class TestTheTwoPathsAgree:
    def test_the_same_path_twice_is_bit_exact(self, runs):
        """The control, and it comes first on purpose.

        Without it a difference between the paths means nothing — it could just
        be a model that isn't deterministic here. gpt2 on one GPU is.
        """
        trusted, control, _ = runs

        for name, value in trusted.items():
            assert torch.equal(value, control[name]), f"{name} is not deterministic"

    @pytest.mark.parametrize("name", ["hidden", "logits", "made"])
    def test_trusted_and_untrusted_are_bit_exact(self, runs, name):
        trusted, _, untrusted = runs
        expected, actual = trusted[name], untrusted[name]

        assert actual.dtype == expected.dtype, (
            f"{name} is {expected.dtype} trusted and {actual.dtype} untrusted — "
            "the two paths are not applying the same autocast"
        )
        assert torch.equal(actual, expected), (
            f"{name} differs between the paths by at most "
            f"{(actual.float() - expected.float()).abs().max().item():.3e}"
        )

    def test_a_block_made_tensor_is_cast_like_the_model(self, runs):
        # Stated separately from the equality above because it is the specific
        # thing that broke: `hidden.float() @ ...` is float32 arithmetic that
        # autocast brings back to the model's dtype. Untrusted, with no region,
        # it stayed float32 and nothing complained.
        trusted, _, untrusted = runs

        assert trusted["made"].dtype == trusted["hidden"].dtype
        assert untrusted["made"].dtype == untrusted["hidden"].dtype
