"""Errors cross the sandbox boundary as themselves, not as pipe noise (#280).

The issue's shape: an out-of-order access — the most common user mistake — was
diagnosed as ``OutOfOrderError`` on the trusted path and reported as
``BrokenPipeError: [Errno 32] Broken pipe`` on the untrusted one. The host
THROWs the classification into the dangling worker, the runner raises it,
reports EXCEPTION and exits — and the host's next send (DONE, or a second
THROW) hit the dead socket and the pipe error masked the real one. #305's
barrier work made those sends tolerant (``driver.interleave`` /
``check_dangling`` swallow ``OSError`` and let ``pump`` read the buffered
EXCEPTION — AF_UNIX keeps buffered bytes after peer exit), which fixed the
client-facing half; the first two tests here pin that, since no test covered
the plain repro.

Two faces were *not* covered by #305 and are fixed alongside these tests:

* **Telemetry truth.** The host wraps the runner's report in ``RunnerError``,
  and ``report`` recorded ``error_type=RunnerError`` where the trusted path
  recorded ``OutOfOrderError`` — the issue's misattribution, surviving in a
  milder form. The runner now ships the terminal exception's class name on the
  EXCEPTION message (``nns._run``), ``RunnerError`` carries it as
  ``cause_type``, and ``SandboxHost.error_name`` hands it to ``report``.
* **A failure the host raises itself** — applying a bad swapped value such that
  the forward step raises host-side — must surface as that exception, never as
  whatever pipe noise follows when the runner's socket goes down. This already
  holds (the host's own exception propagates out of ``pump`` to
  ``format_error``; the runner's BrokenPipe stays in the runner), and the
  tests pin it, trusted run as ground truth.

Same stack-free harness as ``test_sandbox_iter_pins`` (#296) /
``test_sandbox_barriers`` (#294), whose helpers this imports: no server and no
Ray — a real gpt2 on CPU, a runner spawned over a Unix socket, a
``SandboxDriver`` pumping it.
"""

from __future__ import annotations

import logging

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from nnsight.intervention.interleaver import OutOfOrderError

from ndif.services.ray.sandbox.driver import RunnerError

from test_sandbox_iter_pins import (  # noqa: F401  (model is a fixture)
    Capture,
    model,
    run_sandboxed,
)

PROMPT = "the " * 64
PROMPT2 = "The quick brown fox jumps over the"

OOO_MESSAGE = (
    "'model.transformer.h.0.output.i0' was requested but the model already "
    "ran past it"
)


def sandboxed_error(model, build):
    """Serialize ``build``'s trace, run it through a real runner, and return the
    exception the host surfaces (what ``format_error`` would be handed). The
    capture backend serializes without executing, so only the sandboxed run
    raises."""
    capture = Capture()
    build(capture)
    with pytest.raises(Exception) as caught:
        run_sandboxed(model, capture.blob)
    return caught.value


class TestOutOfOrder:
    """The issue's repro: the host THROWs into a dangling worker (face a)."""

    def build(self, model, backend):
        kwargs = {"backend": backend} if backend else {}
        with model.trace(PROMPT, **kwargs):
            model.lm_head.output.save()              # runs LAST
            model.transformer.h[0].output[0].save()  # runs FIRST -> passed

    def test_trusted_ground_truth(self, model):
        with pytest.raises(OutOfOrderError, match="already ran past it"):
            self.build(model, None)

    def test_reports_the_out_of_order_error_not_pipe_noise(self, model):
        # Before #305 this surfaced as BrokenPipeError: the runner raised the
        # thrown OutOfOrderError, reported EXCEPTION and exited, and the host's
        # DONE send hit the dead socket first.
        error = sandboxed_error(model, lambda backend: self.build(model, backend))
        assert isinstance(error, RunnerError), repr(error)
        assert OOO_MESSAGE in str(error)
        assert "BrokenPipe" not in str(error)

    def test_telemetry_records_the_real_type(self, model):
        # The trusted path records error_type=OutOfOrderError for this block;
        # the sandboxed path recorded the RunnerError wrapper. The terminal
        # type's name now rides the EXCEPTION message as `cause_type`.
        error = sandboxed_error(model, lambda backend: self.build(model, backend))
        assert isinstance(error, RunnerError), repr(error)
        assert error.cause_type == "OutOfOrderError"


class TestHostSideApplyFailure:
    """A swapped value the host cannot run with (face b): the forward raises on
    the *host*, and the actor must report that — the cause it already holds —
    rather than the transport symptoms that follow."""

    def build(self, model, backend):
        kwargs = {"backend": backend} if backend else {}
        with model.trace(PROMPT, **kwargs):
            model.transformer.h[0].output = (torch.ones(3, 3),)
            model.lm_head.output.save()

    def expected(self, model):
        with pytest.raises(Exception) as caught:
            self.build(model, None)
        return caught.value

    def test_reports_the_hosts_own_exception(self, model):
        # The host's forward dies applying the bad value (gpt2's next layer norm
        # rejects the tuple). That exception propagates out of `pump` to the
        # actor as itself; the runner's subsequent BrokenPipe stays in the
        # runner. The trusted run is the ground truth for the type.
        expected = self.expected(model)
        error = sandboxed_error(model, lambda backend: self.build(model, backend))
        assert type(error) is type(expected), repr(error)
        assert str(error) == str(expected)
        assert "BrokenPipe" not in str(error)

    def test_widen_failure_in_a_batched_invoke(self, model):
        # The batched variant: the bad value fails in Batcher.widen while being
        # spliced into the combined batch — still the host's own exception.
        def build(backend):
            kwargs = {"backend": backend} if backend else {}
            with model.trace(**kwargs) as tracer:
                with tracer.invoke(PROMPT):
                    model.transformer.h[0].output = (torch.ones(5, 7, 9),)
                    model.output.logits[0, -1].argmax().save()
                with tracer.invoke(PROMPT2):
                    model.output.logits[0, -1].argmax().save()

        with pytest.raises(Exception) as trusted:
            build(None)
        error = sandboxed_error(model, build)
        assert type(error) is type(trusted.value), repr(error)
        assert "BrokenPipe" not in str(error)


class TestBlockErrorsStayFaithful:
    """The faithful 14/15 from the issue's scoping comment stay faithful, and
    now carry the cause's type name for telemetry."""

    def test_a_user_raise_while_another_invoke_is_parked(self, model):
        # Invoke 1 raises mid-block while invoke 2 is still parked on a later
        # location: the host is waiting on the runner when it dies, so the
        # buffered EXCEPTION is read, not a broken send.
        def build(backend):
            kwargs = {"backend": backend} if backend else {}
            with model.trace(**kwargs) as tracer:
                with tracer.invoke(PROMPT):
                    model.transformer.h[0].output
                    raise ValueError("user mistake in invoke 1")
                with tracer.invoke(PROMPT2):
                    model.output.logits[0, -1].argmax().save()

        error = sandboxed_error(model, build)
        assert isinstance(error, RunnerError), repr(error)
        assert "user mistake in invoke 1" in str(error)
        assert error.cause_type == "ValueError"
        assert "BrokenPipe" not in str(error)


class TestReportWiring:
    """`report` records the shipped cause type as error_type — the line the
    issue's telemetry table shows diverging between the two paths.

    The deployment class lives behind `import ray`, which the sandbox harness
    otherwise never needs, so these two skip where ray isn't installed."""

    def test_error_type_is_the_cause(self, monkeypatch, caplog):
        from types import SimpleNamespace

        pytest.importorskip("ray")
        from ndif.services.ray.deployments.modeling.base import (
            ExecutionTimeMetric,
        )
        from ndif.services.ray.sandbox.model import SandboxModelDeployment

        monkeypatch.setattr(
            ExecutionTimeMetric, "update", classmethod(lambda cls, **kw: None)
        )
        deployment = object.__new__(SandboxModelDeployment)
        deployment.model_key = "test-model"
        request = SimpleNamespace(id="r1", api_key="k", email="e@example.com")
        error = RunnerError("Traceback ...", cause_type="OutOfOrderError")

        with caplog.at_level(logging.ERROR, logger="ndif.modeling"):
            deployment.report(request, "error", 1.0, exception=error)

        record = next(
            r for r in caplog.records if r.getMessage() == "model execution errored"
        )
        assert record.error_type == "OutOfOrderError"

    def test_a_bare_runner_error_still_names_itself(self, monkeypatch, caplog):
        # An EXCEPTION from an older runner build carries no cause_type; the
        # wrapper's own name is still better than crashing or guessing.
        from types import SimpleNamespace

        pytest.importorskip("ray")
        from ndif.services.ray.deployments.modeling.base import (
            ExecutionTimeMetric,
        )
        from ndif.services.ray.sandbox.model import SandboxModelDeployment

        monkeypatch.setattr(
            ExecutionTimeMetric, "update", classmethod(lambda cls, **kw: None)
        )
        deployment = object.__new__(SandboxModelDeployment)
        deployment.model_key = "test-model"
        request = SimpleNamespace(id="r1", api_key="k", email="e@example.com")

        with caplog.at_level(logging.ERROR, logger="ndif.modeling"):
            deployment.report(request, "error", 1.0, exception=RunnerError("x"))

        record = next(
            r for r in caplog.records if r.getMessage() == "model execution errored"
        )
        assert record.error_type == "RunnerError"
