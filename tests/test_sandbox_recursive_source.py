"""Recursive ``.source`` works on the sandbox path (#281, last comment).

One-level ``.source`` crosses the socket as a SOURCE control park and was fixed
in 413ba0b; drilling *into* an op — ``attn.source.attention_interface_1.source``
— still died in the runner with ``SourceNotAvailable: recursive `.source` is
only available inside a trace``, blocking the documented route to eager
attention probabilities (``attention_interface_1.source.attn_weights_2``).

Two processes, three faults:

* the ``SourceEnvoy`` the runner's ``IPCSource`` hands out is the plain base
  class, whose ``.source`` reads ``self.envoy.interleaver`` — in the runner
  that is a never-entered unpickling copy (each persistent ``"Interleaver"`` id
  loads a fresh ``IPCInterleaver``), so ``interleaving`` is ``False`` mid-trace
  and the property raised its outside-a-trace error;
* had it not, the base property marks ``interleaver.sourced[path] = None`` and
  builds the instrumented callable *in its own process* — while ``run_op``
  consults the host's interleaver, whose forward is the one that runs, so the
  op's ``.fn`` would never have been served and the inner ops never fired;
* and the host's ``install_source`` resolved every SOURCE path with
  ``_envoy_at``, which walks module attributes and cannot take a nested
  ``...attn.source.attention_interface_1`` path at all.

The fix splits the base property at the same seam as everything else: the
runner's patched ``SourceEnvoy.source`` (``ipc_recursive_source``, nns.py) arms
the host over a SOURCE control park carrying the op path, then parks on
``{path}.fn`` exactly as the base does; the host (``install_source`` +
``SandboxDriver.build_recursive_source``, driver.py) marks its own interleaver
and, when the op fires and ``run_op`` serves the live callable to that park,
instruments it host-side so the forward runs the instrumented copy and the
inner ops become ordinary locations the proxies serve.

Same stack-free harness as ``test_sandbox_iter_pins`` (#303), whose helpers
this imports — no server, no Ray: a real gpt2 on CPU, a spawned runner, a
``SandboxDriver`` over the socket, the trusted/local run as ground truth. The
model here is its own fixture because the documented probabilities route needs
``attn_implementation="eager"`` (the default sdpa lowers the softmax into a C
kernel). The repro tests catch the error *inside the block*, as the issue's
repro does, so an unfixed run fails the assertion with the server's exact
error text rather than a masked end-to-end failure.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from test_sandbox_iter_pins import (  # noqa: F401
    PROMPT,
    REPO,
    Capture,
    run_sandboxed,
)

GENERATE = dict(max_new_tokens=3, min_new_tokens=3, do_sample=False)


@pytest.fixture(scope="module")
def model():
    """A dispatched gpt2 on CPU with **eager** attention, so the attention
    interface the trace drills into is the Python one whose ``attn_weights_2``
    is the post-softmax probabilities — the route the issue names."""
    from nnsight import TransformersModel

    try:
        return TransformersModel(
            REPO,
            task="text-generation",
            device="cpu",
            dispatch=True,
            attn_implementation="eager",
        )
    except Exception as error:  # no cached weights and no network, most likely
        pytest.skip(f"could not load {REPO}: {error}")


def both_ways(model, block):
    """Run ``block`` locally (ground truth) and sandboxed; return both dicts.

    ``block`` takes ``(model, tracer, vals)`` and stores what it keeps in
    ``vals``. (The ``with model.trace(...)`` stays written out at each call
    site — nnsight reads the ``with`` off the calling line, so a trace opened
    through a helper lambda would run eagerly instead.)
    """
    with model.trace(PROMPT) as tracer:
        vals = nnsight.save({})
        block(model, tracer, vals)
    local = snapshot(vals)

    capture = Capture()
    with model.trace(PROMPT, backend=capture) as tracer:
        vals = nnsight.save({})
        block(model, tracer, vals)

    return local, run_sandboxed(model, capture.blob)["vals"]


def snapshot(vals):
    return {
        key: value.detach().clone() if isinstance(value, torch.Tensor) else value
        for key, value in vals.items()
    }


def assert_matches(local, sandboxed):
    # An "error" key is the block's own capture of a raised exception — the
    # issue's repro shape — so an unfixed run fails here printing the server's
    # real error text (`SourceNotAvailable: recursive `.source` is only
    # available inside a trace`) instead of an opaque end-to-end failure.
    assert "error" not in local, local.get("error")
    assert "error" not in sandboxed, sandboxed.get("error")
    assert set(sandboxed) == set(local)
    for key, expected in local.items():
        actual = sandboxed[key]
        if isinstance(expected, torch.Tensor):
            assert torch.allclose(actual, expected), (
                f"'{key}' diverged from the trusted run"
            )
        else:
            assert actual == expected, f"'{key}' diverged from the trusted run"


class TestRecursiveSource:
    """The issue's repro: drill into the attention interface and read an op."""

    def test_eager_attention_probabilities(self, model):
        # The documented route: attention_interface_1.source.attn_weights_2 is
        # the post-softmax attention. Fail-on-unfixed verified: before the fix
        # the block captures the runner's SourceNotAvailable into vals["error"].
        def block(model, tracer, vals):
            try:
                inner = model.transformer.h[0].attn.source.attention_interface_1.source
                vals["names"] = list(inner.names)
                vals["probs"] = inner.attn_weights_2.output
            except BaseException as error:
                vals["error"] = f"{type(error).__name__}: {error}"

        local, sandboxed = both_ways(model, block)
        assert_matches(local, sandboxed)
        # And they really are probabilities — rows sum to one.
        assert torch.allclose(
            sandboxed["probs"].sum(-1), torch.ones_like(sandboxed["probs"].sum(-1))
        )

    def test_one_level_source_is_unchanged(self, model):
        # Guards the 413ba0b fix this builds on: a plain module-level source op
        # read still crosses as before, with the same names the local path sees.
        def block(model, tracer, vals):
            try:
                source = model.transformer.h[0].mlp.source
                vals["names"] = list(source._names if hasattr(source, "_names") else source.names)
                vals["act"] = source.self_act_0.output
            except BaseException as error:
                vals["error"] = f"{type(error).__name__}: {error}"

        assert_matches(*both_ways(model, block))

    def test_beside_module_output_reads(self, model):
        # The #296-shaped hazard: the arming SOURCE park and the `.fn` park land
        # between module reads in one block, so a stale pin push (or a stolen
        # occurrence) would derail the later read. Module reads bracket the
        # recursive drill on both sides.
        def block(model, tracer, vals):
            try:
                vals["h0"] = model.transformer.h[0].output[0].sum()
                inner = model.transformer.h[2].attn.source.attention_interface_1.source
                vals["probs"] = inner.attn_weights_2.output
                vals["h5"] = model.transformer.h[5].output[0].sum()
                vals["ln_f"] = model.transformer.ln_f.output.sum()
            except BaseException as error:
                vals["error"] = f"{type(error).__name__}: {error}"

        assert_matches(*both_ways(model, block))

    def test_generate_steps_reuse_the_built_op(self, model):
        # Later fires of a drilled-into op reuse the instrumented copy built on
        # the first (`interleaver.sourced` on the host, run_op's cache) — a
        # tracer.iter loop reads it every step, across KV-cached steps whose
        # shapes differ from step 0's. Mirrors nnsight's own
        # test_nested_source_across_generate_steps, over the socket.
        def block(model, tracer, vals):
            attn = model.transformer.h[0].attn
            for step in tracer.iter[:3]:
                vals[f"step{step}"] = (
                    attn.source.attention_interface_1.source.attn_weights_2.output
                )

        with model.generate(PROMPT, **GENERATE) as tracer:
            vals = nnsight.save({})
            block(model, tracer, vals)
        local = snapshot(vals)

        capture = Capture()
        with model.generate(PROMPT, backend=capture, **GENERATE) as tracer:
            vals = nnsight.save({})
            block(model, tracer, vals)
        sandboxed = run_sandboxed(model, capture.blob)["vals"]

        assert_matches(local, sandboxed)
        assert sandboxed["step0"].shape[-2] > 1  # full prompt
        assert sandboxed["step1"].shape[-2] == 1  # KV-cached steps
        assert sandboxed["step2"].shape[-2] == 1


class TestMissingOpErrorStory:
    """An op that doesn't exist fails with the available names, like one-level."""

    def test_recursive_missing_op_lists_available_names(self, model):
        def block(model, tracer, vals):
            inner = model.transformer.h[0].attn.source.attention_interface_1.source
            try:
                inner.nope_0.output
            except AttributeError as error:
                vals["error_text"] = str(error)
            # Keep the worker honest: read something real so the trace completes.
            vals["probs"] = inner.attn_weights_2.output

        local, sandboxed = both_ways(model, block)
        assert sandboxed["error_text"] == local["error_text"]
        assert "has no operation 'nope_0'" in sandboxed["error_text"]
        assert "attn_weights_2" in sandboxed["error_text"]  # the available list

    def test_one_level_missing_op_parity(self, model):
        # The same story one level up, so the two paths' wording stays aligned.
        def block(model, tracer, vals):
            try:
                model.transformer.h[0].mlp.source.nope_0.output
            except AttributeError as error:
                vals["error_text"] = str(error)

        local, sandboxed = both_ways(model, block)
        assert sandboxed["error_text"] == local["error_text"]
        assert "has no operation 'nope_0'" in sandboxed["error_text"]
        assert "self_act_0" in sandboxed["error_text"]
