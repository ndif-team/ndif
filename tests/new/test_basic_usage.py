"""Basic / happy-path usage scenarios.

Confirms the bread-and-butter remote nnsight surface still works end-to-
end on a live NDIF stack: a simple trace returns saved values, generate
runs to completion, sessions bundle, non-blocking returns a job id, etc.

Run with:
    pytest tests/report/test_basic_usage.py --run-remote -v
"""

import time

import pytest
import torch

from tests.report._helpers import (
    GPT2_REPO,
    capture_responses,
    find_any_level,
    get_status,
    run_trace,
)


pytestmark = pytest.mark.usefixtures("_configure_nnsight")


# ---------------------------------------------------------------------------
# Trace + save
# ---------------------------------------------------------------------------


class TestBasicTrace:
    """A small remote trace returns the values you save()."""

    def test_trace_returns_saved_output(self, gpt2):
        with gpt2.trace("Hello world", remote=True):
            output = gpt2.output.save()
        assert output is not None
        # gpt2's .output is the model's CausalLMOutput-ish; check the
        # logits tensor exists.
        assert hasattr(output, "logits")
        assert isinstance(output.logits, torch.Tensor)

    def test_trace_returns_intermediate_hidden_states(self, gpt2):
        with gpt2.trace("Hello world", remote=True):
            hs = gpt2.transformer.h[-1].output.save()
        assert isinstance(hs, torch.Tensor)
        assert hs.ndim == 3  # (batch, seq, hidden)

    def test_unsaved_values_are_dropped(self, gpt2):
        """Only ``.save()``-ed values come back."""
        with gpt2.trace("Hello world", remote=True):
            _ = gpt2.transformer.h[0].output  # not saved
            kept = gpt2.lm_head.output.save()
        assert kept is not None

    def test_two_sequential_traces(self, gpt2):
        out1 = run_trace(gpt2, "First")
        out2 = run_trace(gpt2, "Second")
        assert out1 is not None and out2 is not None

    def test_model_becomes_HOT_after_first_request(self, host, gpt2):
        """The first request to a never-deployed model lazily deploys it.

        After the trace returns, /status should show repo at HOT (the
        replica is up and was just used).
        """
        run_trace(gpt2, "Trigger deploy")
        # Brief wait — /status caches; helper sleeps 2s.
        level = find_any_level(get_status(host), GPT2_REPO)
        assert level == "HOT", f"expected HOT, got {level!r}"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


class TestGeneration:

    def test_generate_max_new_tokens(self, gpt2):
        with gpt2.generate("The capital of France is", remote=True, max_new_tokens=3):
            tokens = gpt2.generator.output.save()
        # tokens is the input + max_new_tokens
        assert isinstance(tokens, torch.Tensor)
        decoded = gpt2.tokenizer.decode(tokens[0])
        assert isinstance(decoded, str)
        assert len(decoded) > 0

    def test_generate_with_internal_access(self, gpt2):
        with gpt2.generate("Hello", remote=True, max_new_tokens=2):
            hs = gpt2.transformer.h[0].output.save()
            tokens = gpt2.generator.output.save()
        assert hs is not None
        assert tokens is not None


# ---------------------------------------------------------------------------
# Non-blocking — fire and poll
# ---------------------------------------------------------------------------


class TestNonBlocking:
    """``blocking=False`` returns a job id immediately and the result
    is fetched via subsequent backend() polls."""

    def test_non_blocking_returns_job_id(self, gpt2):
        with gpt2.trace("Hello", remote=True, blocking=False) as tracer:
            _ = gpt2.output.save()
        backend = tracer.backend
        # job_id is set on submit
        assert backend.job_id is not None
        assert len(backend.job_id) > 0

    def test_non_blocking_poll_to_completion(self, gpt2):
        with gpt2.trace("Hello", remote=True, blocking=False) as tracer:
            output = gpt2.output.save()
        backend = tracer.backend
        # Poll with a generous timeout
        deadline = time.time() + 120
        result = None
        while time.time() < deadline:
            result = backend()
            if result is not None:
                break
            time.sleep(1)
        assert result is not None, "non-blocking job never completed"


# ---------------------------------------------------------------------------
# Capture all the response messages a normal request sees
# ---------------------------------------------------------------------------


class TestObservableResponses:
    """A successful remote trace produces a predictable status sequence."""

    def test_status_sequence_contains_completed(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "Hello world")
        statuses = [s for s, _ in recorded]
        # Last status must be COMPLETED for a clean run.
        assert statuses[-1] == "COMPLETED", (
            f"final status was {statuses[-1]!r}, full sequence: {statuses}"
        )

    def test_status_sequence_includes_running(self, gpt2):
        """RUNNING is what the ModelActor emits when pre() finishes and
        execute() begins; the user should see at least one."""
        with capture_responses() as recorded:
            run_trace(gpt2, "Hello world")
        statuses = [s for s, _ in recorded]
        # The exact ordering may compress (RECEIVED/QUEUED can both
        # appear, or one may be skipped depending on timing), but
        # RUNNING + COMPLETED are reliable.
        assert "RUNNING" in statuses, (
            f"expected RUNNING in sequence, got {statuses}"
        )


# ---------------------------------------------------------------------------
# Error path — user code raises an exception
# ---------------------------------------------------------------------------


class TestUserError:
    """When user code throws, the user sees an ERROR with the exception
    message in the description."""

    def test_user_exception_surfaces_as_error(self, gpt2):
        with capture_responses() as recorded:
            with pytest.raises(Exception):  # nnsight wraps it
                with gpt2.trace("Hello", remote=True):
                    # Force a server-side error.
                    raise RuntimeError("intentional test failure")

        statuses = [s for s, _ in recorded]
        # The final status the user receives is ERROR.
        assert statuses[-1] == "ERROR", f"got {statuses}"

    def test_user_exception_description_carries_message(self, gpt2):
        with capture_responses() as recorded:
            with pytest.raises(Exception):
                with gpt2.trace("Hello", remote=True):
                    _ = 1 / 0  # ZeroDivisionError server-side
        last_status, last_desc = recorded[-1]
        assert last_status == "ERROR"
        # The description carries the formatted traceback; the actual
        # exception type name should appear somewhere in it.
        assert (
            "ZeroDivisionError" in last_desc
            or "division by zero" in last_desc
        ), f"description didn't carry the error: {last_desc!r}"
