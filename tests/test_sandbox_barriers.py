"""``tracer.barrier()`` works across the socket (#294).

The base ``Barrier`` counts arrivals inside the process running the blocks and
the last arrival releases the others by switching them directly. In the sandbox
all the blocks live in the runner, so that release *worked* — runner-side — but
the host was never told: the released worker's next park was stashed on its own
mediator (``other.pending = other.switch()`` in ``Barrier.__call__``) and never
crossed the socket, so the host proxy kept the stale BARRIER park, every
post-barrier read went unserved, and the run ended with that worker dangling.
Worse, the dangling check read the barrier park's ``None`` iteration as a
``tracer.iter`` overrun, so the job COMPLETED with a loop warning
(``'None.i0' was never reached``) and the worker's saves silently missing.

The fix is host-authority barriers, matching where iteration authority already
lives: the runner's patched ``Barrier.__call__`` parks *every* arrival over the
socket with the barrier's identity and count, and the host
(``SandboxDriver.barrier_arrival``) counts and releases the round — each release
a RESUME→PARK round trip, so a released worker's next park always crosses. A
genuinely wrong count now fails loudly: ``check_dangling`` classifies a BARRIER
park as the base does and the runner raises the same ValueError the trusted
path does, instead of the loop warning.

Same harness as ``test_sandbox_iter_pins`` (#303), whose helpers this imports:
no server and no Ray — a real gpt2 on CPU, a runner spawned over a Unix socket,
a ``SandboxDriver`` pumping it, and the trusted run as ground truth. Every
value-shaped test compares against the local run, because the failure mode here
was a *silently missing* save: "no exception" passed before the fix.
"""

from __future__ import annotations

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from ndif.services.ray.sandbox.driver import RunnerError

from test_sandbox_iter_pins import (  # noqa: F401  (model is a fixture)
    PROMPT,
    Capture,
    model,
    run_sandboxed,
)

PROMPT2 = "The quick brown fox jumps over the"


class TestCrossInvokeBarrier:
    """Barriers that release mid-forward — the shapes #294 dropped saves on."""

    def test_the_issues_repro(self, model):
        # Invoke 1 touches the model *before* the barrier, so invoke 2's release
        # happens mid-forward, runner-side only before the fix: the job
        # COMPLETED, `a` came back, `b` silently never existed, and the only
        # signal was a loop warning about 'None.i0'.
        with model.trace() as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke(PROMPT):
                model.transformer.h[0].output
                barrier()
                a = model.output.logits[0, -1].argmax().save()
            with tracer.invoke(PROMPT2):
                barrier()
                b = model.output.logits[0, -1].argmax().save()
        local = {"a": a.clone(), "b": b.clone()}

        capture = Capture()
        with model.trace(backend=capture) as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke(PROMPT):
                model.transformer.h[0].output
                barrier()
                a = model.output.logits[0, -1].argmax().save()
            with tracer.invoke(PROMPT2):
                barrier()
                b = model.output.logits[0, -1].argmax().save()

        saved = run_sandboxed(model, capture.blob)
        assert set(saved) >= {"a", "b"}, (
            f"a save was silently dropped: {sorted(saved)}"
        )
        assert torch.equal(saved["a"], local["a"])
        assert torch.equal(saved["b"], local["b"])

    def test_the_embedding_transplant(self, model):
        # nnsight's own Barrier docstring pattern: one invoke reads the wte
        # embeddings, the other swaps them in over an underscore prompt. The
        # released worker's SWAP must land on the *same* wte visit the host is
        # still serving (and be widened to its own rows), so both continuations
        # follow the meaningful prompt. Before the fix the swap never crossed.
        kwargs = dict(max_new_tokens=3, do_sample=False)

        with model.generate(**kwargs) as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke("Madison Square Garden is in the city of"):
                embeddings = model.transformer.wte.output
                barrier()
                tokens = tracer.result.save()
            with tracer.invoke("_ _ _ _ _ _ _ _ _"):
                barrier()
                model.transformer.wte.output = embeddings
        local = tokens.clone()

        capture = Capture()
        with model.generate(backend=capture, **kwargs) as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke("Madison Square Garden is in the city of"):
                embeddings = model.transformer.wte.output
                barrier()
                tokens = tracer.result.save()
            with tracer.invoke("_ _ _ _ _ _ _ _ _"):
                barrier()
                model.transformer.wte.output = embeddings

        saved = run_sandboxed(model, capture.blob)
        assert "tokens" in saved, f"the save was dropped: {sorted(saved)}"
        assert torch.equal(saved["tokens"], local)

    def test_a_barrier_before_any_read(self, model):
        # Every block reaches the barrier before touching the model, so the
        # whole round completes among the *initial* parks — the one shape that
        # accidentally worked before the fix (the round finished runner-side
        # during worker start-up, before anything crossed). It must keep
        # working now that the round completes host-side in _build_proxies,
        # before the forward runs.
        with model.trace() as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke(PROMPT):
                barrier()
                a = model.output.logits[0, -1].argmax().save()
            with tracer.invoke(PROMPT2):
                barrier()
                b = model.output.logits[0, -1].argmax().save()
        local = {"a": a.clone(), "b": b.clone()}

        capture = Capture()
        with model.trace(backend=capture) as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke(PROMPT):
                barrier()
                a = model.output.logits[0, -1].argmax().save()
            with tracer.invoke(PROMPT2):
                barrier()
                b = model.output.logits[0, -1].argmax().save()

        saved = run_sandboxed(model, capture.blob)
        assert torch.equal(saved["a"], local["a"])
        assert torch.equal(saved["b"], local["b"])

    def test_reuse_across_two_rounds(self, model):
        # One barrier, two rounds (base empties `_waiting` per release, so a
        # barrier is reusable). Round 1 releases mid-forward; both workers
        # immediately re-arrive, so round 2's arrivals land *during* round 1's
        # release — the host must have cleared the round before resuming anyone,
        # or the second round double-counts the first's arrivals.
        with model.trace() as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke(PROMPT):
                model.transformer.h[0].output
                barrier()
                barrier()
                a = model.output.logits[0, -1].argmax().save()
            with tracer.invoke(PROMPT2):
                barrier()
                barrier()
                b = model.output.logits[0, -1].argmax().save()
        local = {"a": a.clone(), "b": b.clone()}

        capture = Capture()
        with model.trace(backend=capture) as tracer:
            barrier = tracer.barrier(2)
            with tracer.invoke(PROMPT):
                model.transformer.h[0].output
                barrier()
                barrier()
                a = model.output.logits[0, -1].argmax().save()
            with tracer.invoke(PROMPT2):
                barrier()
                barrier()
                b = model.output.logits[0, -1].argmax().save()

        saved = run_sandboxed(model, capture.blob)
        assert torch.equal(saved["a"], local["a"])
        assert torch.equal(saved["b"], local["b"])


class TestWrongCount:
    """A count no set of blocks can satisfy fails loudly, like the trusted path."""

    MESSAGE = "A barrier was never reached by every block it waits for"

    def test_too_high_a_count_raises_the_base_error_both_ways(self, model):
        # n=3 with two invokes: the round can never release. The trusted path
        # raises a clear ValueError out of the dangling check; the sandbox must
        # ship the same error — before the fix it mistook the barrier park for
        # a tracer.iter overrun, *warned*, and COMPLETED with the saves gone.
        with pytest.raises(ValueError, match=self.MESSAGE):
            with model.trace() as tracer:
                barrier = tracer.barrier(3)
                with tracer.invoke(PROMPT):
                    barrier()
                    model.output.logits[0, -1].argmax().save()
                with tracer.invoke(PROMPT2):
                    barrier()
                    model.output.logits[0, -1].argmax().save()

        capture = Capture()
        with model.trace(backend=capture) as tracer:
            barrier = tracer.barrier(3)
            with tracer.invoke(PROMPT):
                barrier()
                model.output.logits[0, -1].argmax().save()
            with tracer.invoke(PROMPT2):
                barrier()
                model.output.logits[0, -1].argmax().save()

        # The runner formats the traceback and ships text, so the assertion is
        # on the message — the base ValueError's wording, not the iter warning.
        with pytest.raises(RunnerError, match=self.MESSAGE):
            run_sandboxed(model, capture.blob)
