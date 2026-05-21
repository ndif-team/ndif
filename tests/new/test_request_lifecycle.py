"""Capture the canonical request status sequences a user sees.

These tests pin down the literal strings the NDIF backend sends over
Socket.IO at each request stage. The captured sequences feed directly
into ``report.md`` — if you change a status description anywhere on the
server, expect one of these to fail with a useful diff.

Run with:
    pytest tests/report/test_request_lifecycle.py --run-remote -v
"""

import pytest

from tests.report._helpers import (
    GPT2_REPO,
    capture_responses,
    evict_via_lib,
    run_trace,
    wait_for_no_replicas,
)


# ---------------------------------------------------------------------------
# Cold-start (no replicas) vs warm trace sequences
# ---------------------------------------------------------------------------


class TestColdStartSequence:
    """First-ever request to a model that isn't deployed.

    The Processor walks through internal states PROVISIONING and
    DEPLOYING before READY, but the **user-visible** response status
    stays at ``QUEUED`` throughout — what changes is the description:

        "Model Provisioning..."  (Processor in PROVISIONING)
        "Model Deploying..."     (Processor in DEPLOYING)
        "Moved to position N in Queue."  (Processor READY but request not
                                          yet at the head of the queue)
    """

    @pytest.fixture(autouse=True)
    def _evict_first(self, host):
        """Make sure gpt2 has no replicas before this class runs.

        The extra sleep after wait_for_no_replicas gives the API-side
        Dispatcher time to drain its eviction_queue and remove the
        gpt2 Processor entirely. The Dispatcher's main loop wakes every
        ~10s via the redis ``brpop`` timeout, and only then does it call
        ``handle_evictions`` to actually pop the gpt2 Processor from
        ``self.processors`` and purge it. Without this wait, a fresh
        request can land on the just-cancelled Processor and see
        ``"Model deployment evicted."`` instead of cold-starting cleanly.
        """
        import time

        evict_via_lib(GPT2_REPO)
        wait_for_no_replicas(host, GPT2_REPO, timeout=60)
        time.sleep(12)

    def test_cold_start_status_sequence(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "Cold start")
        statuses = [s for s, _ in recorded]

        # The cold path is one RECEIVED + multiple QUEUED (with shifting
        # descriptions) + DISPATCHED + RUNNING + COMPLETED.
        assert statuses[0] == "RECEIVED", statuses
        assert "QUEUED" in statuses, statuses
        assert "DISPATCHED" in statuses, statuses
        assert "RUNNING" in statuses, statuses
        assert statuses[-1] == "COMPLETED", statuses
        # Multiple QUEUED frames — at least the per-stage descriptions
        # (Provisioning / Deploying / position) get a re-broadcast each.
        assert statuses.count("QUEUED") >= 2, (
            f"cold start should re-broadcast QUEUED through the stages, "
            f"got {statuses.count('QUEUED')}: {statuses}"
        )

    def test_cold_start_descriptions(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "Cold start, again")
        # Every (status, description) frame the user got, in order.
        descs = [(s, d) for s, d in recorded if s == "QUEUED"]
        joined = "\n".join(d for _, d in descs)
        # At least one of the QUEUED frames carries "Model Provisioning..."
        # or "Model Deploying..." — the stage labels the Processor emits.
        assert (
            "Model Provisioning..." in joined or "Model Deploying..." in joined
        ), (
            f"expected a Provisioning/Deploying description; got QUEUED "
            f"descriptions: {[d for _, d in descs]}"
        )


# ---------------------------------------------------------------------------
# Warm trace (replica already exists)
# ---------------------------------------------------------------------------


class TestWarmTraceSequence:
    """When the Processor already has a ready replica, the sequence
    skips PROVISIONING / DEPLOYING entirely.
    """

    @pytest.fixture(autouse=True)
    def _warmup(self, gpt2):
        """Ensure gpt2 has at least one HOT replica before each test."""
        run_trace(gpt2, "warmup")

    def test_warm_status_sequence(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "Warm trace")
        statuses = [s for s, _ in recorded]
        assert "RECEIVED" in statuses, statuses
        assert "QUEUED" in statuses, statuses
        assert "DISPATCHED" in statuses, statuses
        assert "RUNNING" in statuses, statuses
        assert statuses[-1] == "COMPLETED", statuses
        # No PROVISIONING/DEPLOYING in the warm path.
        assert "PROVISIONING" not in statuses, (
            f"warm path should not see PROVISIONING: {statuses}"
        )
        assert "DEPLOYING" not in statuses, (
            f"warm path should not see DEPLOYING: {statuses}"
        )

    def test_received_description(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "x")
        by_status = {s: d for s, d in recorded}
        assert by_status["RECEIVED"] == (
            "Your job has been received and is waiting to be queued."
        ), f"RECEIVED text changed: {by_status['RECEIVED']!r}"

    def test_queued_description(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "x")
        by_status = {s: d for s, d in recorded}
        # The queue position is part of the description.
        assert by_status["QUEUED"].startswith("Added to Queue at position "), (
            f"QUEUED text changed: {by_status['QUEUED']!r}"
        )

    def test_dispatched_description(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "x")
        by_status = {s: d for s, d in recorded}
        assert by_status["DISPATCHED"] == (
            "Your job has been sent to the model deployment."
        ), f"DISPATCHED text changed: {by_status['DISPATCHED']!r}"

    def test_running_description(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "x")
        by_status = {s: d for s, d in recorded}
        assert by_status["RUNNING"] == "Your job has started running.", (
            f"RUNNING text changed: {by_status['RUNNING']!r}"
        )

    def test_completed_description(self, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "x")
        by_status = {s: d for s, d in recorded}
        assert by_status["COMPLETED"] == "Your job has been completed.", (
            f"COMPLETED text changed: {by_status['COMPLETED']!r}"
        )


# ---------------------------------------------------------------------------
# LOG status — print() inside a remote trace
# ---------------------------------------------------------------------------


class TestPrintEmitsLog:
    """``print(...)`` inside a remote trace is forwarded as a LOG response
    so the user sees the captured stdout interleaved with status updates.
    """

    def test_print_creates_log_response(self, gpt2):
        with capture_responses() as recorded:
            with gpt2.trace("Hello", remote=True):
                print("hello from inside the trace")
                _ = gpt2.output.save()
        statuses = [s for s, _ in recorded]
        assert "LOG" in statuses, (
            f"print() should produce a LOG response in the sequence: {statuses}"
        )

    def test_print_description_contains_stdout(self, gpt2):
        with capture_responses() as recorded:
            with gpt2.trace("Hello", remote=True):
                print("LOG_MARKER_42")
                _ = gpt2.output.save()
        log_descs = [d for s, d in recorded if s == "LOG"]
        joined = "\n".join(log_descs)
        assert "LOG_MARKER_42" in joined, (
            f"LOG response should carry the printed string; got: {log_descs}"
        )


# ---------------------------------------------------------------------------
# Error sequence — user code throws
# ---------------------------------------------------------------------------


class TestErrorSequence:

    def test_user_error_terminates_with_error(self, gpt2):
        with capture_responses() as recorded:
            with pytest.raises(Exception):
                with gpt2.trace("Hello", remote=True):
                    raise RuntimeError("test failure")
        statuses = [s for s, _ in recorded]
        assert statuses[-1] == "ERROR", statuses

    def test_user_error_description_carries_traceback(self, gpt2):
        with capture_responses() as recorded:
            with pytest.raises(Exception):
                with gpt2.trace("Hello", remote=True):
                    raise RuntimeError("UNIQUE_ERROR_SENTINEL_99")
        last_status, last_desc = recorded[-1]
        assert last_status == "ERROR"
        assert "UNIQUE_ERROR_SENTINEL_99" in last_desc, (
            f"error description should carry the user's exception message; "
            f"got: {last_desc!r}"
        )


# ---------------------------------------------------------------------------
# Queue-position semantics — second concurrent request reports position 2
# ---------------------------------------------------------------------------


class TestQueuePosition:
    """When one request is already running, the next one to land reports
    its position in the queue.
    """

    @pytest.fixture(autouse=True)
    def _warmup(self, gpt2):
        run_trace(gpt2, "warmup")

    def test_second_request_position(self, gpt2):
        import threading

        from tests.report._helpers import run_trace_with_sleep

        # First request: holds the replica busy for ~5s.
        results = {}

        def first():
            try:
                results["a"] = run_trace_with_sleep(gpt2, sleep_s=5)
            except Exception as e:
                results["a"] = e

        t = threading.Thread(target=first, daemon=True)
        t.start()
        # Brief pause so the first request is definitely in flight.
        import time as _t

        _t.sleep(2)

        # Second request: should see position 1 (someone else is dispatched
        # but not in the queue) or position 2 (lined up behind another
        # queued request).
        with capture_responses() as recorded:
            run_trace(gpt2, "second")
        t.join(timeout=30)

        by_status = {s: d for s, d in recorded}
        # We can't assert a specific number here without tighter timing
        # control; assert the format is what we expect.
        assert "QUEUED" in by_status, [s for s, _ in recorded]
        assert by_status["QUEUED"].startswith("Added to Queue at position "), (
            by_status["QUEUED"]
        )
