"""An ad-hoc module call leaves the module's real occurrence alone (#295).

``Envoy.__call__`` owns the ad-hoc call contract: with ``hook=False`` (the
default — the logit lens, ``ln_f(h)``) the module runs the ordinary way with the
trace *stood down* for the duration, so nothing is served and no occurrence is
spent, for the module or anything under it; with ``hook=True`` the trace watches
the call, so a worker parked on a child location is served mid-call. The
host-side ``run_module`` hand-rolled that contract and swapped both branches:

* ``hook=False`` called the bare ``_module.forward`` with ``interleaving`` still
  on, so the controllers counted the ad-hoc visit and consumed the real
  occurrence — a later read of the module's real ``.output`` dangled and raised
  ``OutOfOrderError: 'model.transformer.ln_f.output.i0' was requested but the
  model already ran past it`` (and a composite call stole its *children's*
  occurrences the same way);
* ``hook=True`` called ``Envoy.__call__`` with ``hook`` defaulting to ``False``
  — the stood-down behavior — so the branch meant to be watched was hidden.

The fix deletes the hand-rolled branch and defers to ``Envoy.__call__`` (the
host's Envoy is the real, unpatched class — the IPC patches load only in the
runner), so the sandbox gets the trusted path's behavior by construction.

Same harness as ``test_sandbox_iter_pins`` (#303), whose helpers this imports:
no server and no Ray — a real gpt2 on CPU, a runner spawned over a Unix socket,
a ``SandboxDriver`` pumping it, and the trusted run as ground truth. The repro
tests catch the error *inside the block*, as the issue's repro does, so an
unfixed run fails the assertion with the server's exact error text instead of
the ``BrokenPipeError`` that masks it end to end (#280).
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from test_sandbox_iter_pins import (  # noqa: F401  (model is a fixture)
    PROMPT,
    Capture,
    model,
    run_sandboxed,
)


def both_ways(model, block):
    """Run ``block`` locally (ground truth) and sandboxed; return both dicts.

    ``block`` takes ``(model, vals)`` and stores what it keeps in ``vals``.
    """
    with model.trace(PROMPT):
        vals = nnsight.save({})
        block(model, vals)
    local = {
        key: value.detach().clone() if isinstance(value, torch.Tensor) else value
        for key, value in vals.items()
    }

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        vals = nnsight.save({})
        block(model, vals)

    return local, run_sandboxed(model, capture.blob)["vals"]


def assert_matches(local, sandboxed):
    # An "error" key is the block's own capture of a raised exception — the
    # issue's repro shape — so a failure here prints the server's real error
    # (OutOfOrderError on unfixed code) instead of a masked BrokenPipeError.
    assert "error" not in local, local.get("error")
    assert "error" not in sandboxed, sandboxed.get("error")
    assert set(sandboxed) == set(local)
    for key, expected in local.items():
        assert torch.allclose(sandboxed[key], expected), (
            f"'{key}' diverged from the trusted run"
        )


class TestAdhocCallLeavesOccurrences:
    """``hook=False``: the call must not consume the real visit (#295)."""

    def test_a_leaf_call_then_the_modules_real_output(self, model):
        # The issue's repro: ln_f ad hoc on layer 3's stream, then the *real*
        # ln_f.output later in the forward. Unfixed, the ad-hoc call spent
        # ln_f.output.i0 and the real read raised OutOfOrderError.
        def block(model, vals):
            h = model.transformer.h[3].output
            vals["adhoc"] = model.transformer.ln_f(h)
            vals["h5"] = model.transformer.h[5].output.sum()
            try:
                vals["real"] = model.transformer.ln_f.output
            except BaseException as error:
                vals["error"] = f"{type(error).__name__}: {error}"

        assert_matches(*both_ways(model, block))

    def test_a_composite_call_then_a_childs_real_read(self, model):
        # A whole layer ad hoc: the stood-down flag must reach *children* too —
        # unfixed, the bare forward left them instrumented and the call stole
        # h[4].mlp.output's occurrence.
        def block(model, vals):
            h = model.transformer.h[3].output
            vals["adhoc"] = model.transformer.h[4](h)[0]
            try:
                vals["child"] = model.transformer.h[4].mlp.output
            except BaseException as error:
                vals["error"] = f"{type(error).__name__}: {error}"

        assert_matches(*both_ways(model, block))

    def test_the_adhoc_result_itself(self, model):
        # The call's own output, with no later read: pins the value against the
        # trusted run (and that an ad-hoc call alone doesn't derail the block).
        def block(model, vals):
            h = model.transformer.h[3].output
            vals["adhoc"] = model.transformer.ln_f(h)

        assert_matches(*both_ways(model, block))


class TestWatchedCall:
    """``hook=True``: the trace watches the call, over the socket too."""

    def test_a_watched_call_serves_a_parked_worker(self, model):
        # Invoke 1 parks on the real h[4].mlp.output; invoke 2 calls h[4] ad hoc
        # (on a *scaled* stream, so the watched value provably differs from the
        # real visit's) before the forward reaches layer 4. With hook=True the
        # watched call's mlp visit must serve invoke 1's park — mid-CALL, across
        # the socket. Unfixed, hook=True was stood down and invoke 1 silently
        # got the real forward's value instead.
        with model.trace() as tracer:
            with tracer.invoke(PROMPT):
                watched = model.transformer.h[4].mlp.output.save()
            with tracer.invoke(PROMPT):
                h = model.transformer.h[3].output
                adhoc = model.transformer.h[4](h * 2.0, hook=True)[0].save()
        local = {
            "watched": watched.detach().clone(),
            "adhoc": adhoc.detach().clone(),
        }

        capture = Capture()
        with model.trace(backend=capture) as tracer:
            with tracer.invoke(PROMPT):
                watched = model.transformer.h[4].mlp.output.save()
            with tracer.invoke(PROMPT):
                h = model.transformer.h[3].output
                adhoc = model.transformer.h[4](h * 2.0, hook=True)[0].save()

        sandboxed = run_sandboxed(model, capture.blob)
        assert_matches(local, sandboxed)
