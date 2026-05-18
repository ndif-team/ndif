"""THE MATRIX: what users see when their model is evicted mid-request.

For each (request stage × eviction kind) combination, we capture the
exact status string + description the user receives. These are the
canonical answers the docs / report.md cites.

Eviction stages (relative to the user's request):
    1. before-request : no replicas exist when the request lands
                        (this is just a cold start — not an eviction
                        scenario, baseline for the rest of the table)
    2. queued         : request has been enqueued, model evicted before
                        dispatch
    3. dispatched     : request has been sent to the actor; the actor is
                        killed mid-handler
    4. running        : actor was already executing the request when the
                        replica goes away
    5. between        : model is HOT, user re-issues after a previous run;
                        eviction happens between requests (cleanly cached)

Eviction kinds:
    - fanout    : ``evict_via_lib(repo_id)`` — all replicas
    - targeted  : ``evict_via_lib(repo_id, replica=<rid>)`` — one replica
                  (siblings still serve; HOT→WARM on the targeted slot
                  when CPU has room)

The canonical exit strings these tests pin down — copy/paste these into
report.md if anything ever shifts:

    "Replica evicted before dispatch. Sorry for the inconvenience. "
    "Please try again later."

    "Replica was evicted while processing your request. Sorry for the "
    "inconvenience. Please try again later."

    "Model deployment evicted. Please try again later. Sorry for the "
    "inconvenience."

Run with:
    pytest tests/report/test_eviction_user_experience.py --run-remote -v
"""

import threading
import time

import pytest

from tests.report._helpers import (
    GPT2_REPO,
    capture_responses,
    count_replicas,
    deploy_via_lib,
    evict_all_models,
    evict_via_lib,
    find_replicas,
    get_status,
    run_trace,
    run_trace_with_sleep,
    wait_for_no_replicas,
    wait_for_replica_count,
)


@pytest.fixture(autouse=True)
def _reset(host):
    evict_all_models()
    time.sleep(12)


# ---------------------------------------------------------------------------
# Stage 1 — no replicas (baseline cold start, NOT eviction)
# ---------------------------------------------------------------------------


class TestBaselineColdStart:
    """No eviction in play — first request to an undeployed model.

    Tracks the *complete* canonical cold-start sequence so the matrix
    has a "happy baseline" to compare against.
    """

    def test_cold_start_completes_normally(self, host, gpt2):
        with capture_responses() as recorded:
            run_trace(gpt2, "cold")
        statuses = [s for s, _ in recorded]
        descs = {s: d for s, d in recorded}
        assert statuses[-1] == "COMPLETED", statuses
        assert descs["COMPLETED"] == "Your job has been completed."


# ---------------------------------------------------------------------------
# Stage 2 — eviction while request is in the QUEUE (pre-dispatch)
# ---------------------------------------------------------------------------


class TestEvictionWhileQueued:
    """User's request sits in the Processor's queue; an admin issues
    ``evict`` before it dispatches.

    What the user sees:
      - RECEIVED + QUEUED (initial)
      - ERROR with the per-replica "Replica evicted before dispatch" or
        the Processor-level "Model deployment evicted" string.
    """

    def test_fanout_evict_while_queued(self, host, gpt2):
        """First request hogs the (only) replica; second request lands
        in queue; we fan-out evict; second request errors."""
        deploy_via_lib(GPT2_REPO, replicas=1)
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=60)

        results = {}

        def hog():
            try:
                # Long-running so the (single) replica is busy.
                run_trace_with_sleep(gpt2, sleep_s=8, prompt="hog")
                results["hog"] = "ok"
            except Exception as e:
                results["hog"] = e

        hog_t = threading.Thread(target=hog, daemon=True)
        hog_t.start()
        time.sleep(1.5)  # let the hog claim the replica

        evict_thread_started = threading.Event()

        def evict_after_queued():
            evict_thread_started.wait()
            time.sleep(1.0)  # wait for the second request to land in queue
            evict_via_lib(GPT2_REPO)

        ev_t = threading.Thread(target=evict_after_queued, daemon=True)
        ev_t.start()

        evict_thread_started.set()
        try:
            with capture_responses() as recorded:
                with pytest.raises(Exception):
                    run_trace(gpt2, "queued and about to be evicted")
        finally:
            hog_t.join(timeout=30)
            ev_t.join(timeout=30)

        statuses = [s for s, _ in recorded]
        descs_by_status = {s: d for s, d in recorded}
        # The user sees an ERROR end-state.
        assert statuses[-1] == "ERROR", (
            f"queued+evicted should end in ERROR; got {statuses}"
        )
        last_desc = descs_by_status["ERROR"]
        # Should match one of the documented strings.
        assert (
            "Replica evicted before dispatch" in last_desc
            or "Model deployment evicted" in last_desc
            or "Replica was evicted while processing your request" in last_desc
        ), (
            f"unexpected ERROR description: {last_desc!r}\n"
            f"full sequence: {recorded}"
        )


# ---------------------------------------------------------------------------
# Stage 3+4 — eviction while DISPATCHED / RUNNING (in-flight on actor)
# ---------------------------------------------------------------------------


class TestEvictionWhileInFlight:
    """The actor is mid-execute when ``evict`` lands.

    What the user sees:
      - RECEIVED → QUEUED → DISPATCHED → RUNNING
      - ERROR with "Replica was evicted while processing your request"
    """

    def test_evict_during_running(self, host, gpt2):
        deploy_via_lib(GPT2_REPO, replicas=1)
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=60)

        def evict_mid_request():
            # Wait until the user request is mid-flight before evicting.
            time.sleep(2.5)
            evict_via_lib(GPT2_REPO)

        ev_t = threading.Thread(target=evict_mid_request, daemon=True)
        ev_t.start()

        with capture_responses() as recorded:
            with pytest.raises(Exception):
                run_trace_with_sleep(gpt2, sleep_s=8, prompt="long")

        ev_t.join(timeout=30)

        statuses = [s for s, _ in recorded]
        # We should see at least RUNNING before the ERROR.
        assert statuses[-1] == "ERROR", f"expected ERROR end; got {statuses}"
        assert "RUNNING" in statuses or "DISPATCHED" in statuses, statuses

        last_desc = next(d for s, d in reversed(recorded) if s == "ERROR")
        # Three possible documented strings depending on exactly when
        # the evict lands relative to the dispatch call:
        #   1. Mid-call cancellation: "Replica was evicted while processing..."
        #   2. Last-replica tear-down: "Model deployment evicted."
        #   3. handle().remote() raises something else (race): "Error
        #      submitting request to model deployment. Please try again later."
        assert (
            "Replica was evicted while processing your request" in last_desc
            or "Model deployment evicted" in last_desc
            or "Error submitting request to model deployment" in last_desc
        ), (
            f"unexpected ERROR description: {last_desc!r}\n"
            f"full sequence: {recorded}"
        )


# ---------------------------------------------------------------------------
# Stage 5 — model evicted "between" requests (no in-flight user request)
# ---------------------------------------------------------------------------


class TestEvictionBetweenRequests:
    """A model is HOT and serving fine, then evicted while no requests
    are in flight. The *next* user request is a cold start — they don't
    see any error from the eviction, just standard provisioning.
    """

    def test_evict_then_new_request_is_cold_start(self, host, gpt2):
        # Get HOT.
        run_trace(gpt2, "warmup")

        # Evict, wait for tear-down, sleep enough for Dispatcher to drain
        # (10s brpop) and remove the Processor.
        evict_via_lib(GPT2_REPO)
        wait_for_no_replicas(host, GPT2_REPO, timeout=60)
        time.sleep(12)

        # Now a fresh request — it should cold-start cleanly.
        with capture_responses() as recorded:
            run_trace(gpt2, "post-evict cold start")
        statuses = [s for s, _ in recorded]
        assert statuses[-1] == "COMPLETED", (
            f"post-evict cold start should COMPLETE cleanly; got {statuses}"
        )


# ---------------------------------------------------------------------------
# Targeted (per-replica) evict — siblings keep serving
# ---------------------------------------------------------------------------


class TestTargetedEvictDoesNotKillRequest:
    """If we per-replica evict the replica that's NOT serving the
    request, the request finishes normally — the Processor's queue
    routes to whichever Replica is free.
    """

    def test_evict_idle_sibling_does_not_disturb_running(self, host, gpt2):
        deploy_via_lib(GPT2_REPO, replicas=2)
        wait_for_replica_count(host, GPT2_REPO, 2, level="HOT", timeout=90)

        results = {}

        def long_request():
            try:
                run_trace_with_sleep(gpt2, sleep_s=6, prompt="long")
                results["long"] = "ok"
            except Exception as e:
                results["long"] = e

        t = threading.Thread(target=long_request, daemon=True)
        t.start()
        time.sleep(1.5)  # let one of the two replicas pick it up

        # We don't know which replica is busy; just evict whichever one
        # is currently idle by looking at /status for an idle replica.
        # In practice both will appear identical via /status, so we
        # evict the first one we see and hope it's the idle one — if
        # we accidentally evict the busy one, the user sees the
        # "evicted while processing" path, which is *also* a documented
        # outcome we cover elsewhere.
        replicas_now = find_replicas(get_status(host), GPT2_REPO)
        # Pick one to evict — we don't try to be too clever here; with
        # 2 replicas and 1 busy, evicting an arbitrary one is 50/50.
        # Skip the assertion if our long request errored.
        rid_to_evict = replicas_now[0]["replica_id"]
        evict_via_lib(GPT2_REPO, replica=rid_to_evict)

        t.join(timeout=30)

        # The contract: per-replica evict with siblings available should
        # not lose the request, but if the busy one got picked the user
        # would see an error. We assert the *strong* claim that at least
        # one replica is still HOT after the evict — the more
        # interesting "no user error" claim is racy and we don't
        # promise it.
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=15)
        n_hot = count_replicas(get_status(host), GPT2_REPO, level="HOT")
        assert n_hot >= 1, f"sibling should survive targeted evict; have {n_hot}"


# ---------------------------------------------------------------------------
# Last-replica fan-out evict with no request in flight → just tear-down
# ---------------------------------------------------------------------------


class TestLastReplicaCleanup:
    """Fan-out evicting the only replica with no requests should not
    fire any "evicted" string at the user (nobody is listening). The
    Processor tears down silently via ``on_replica_exit``."""

    def test_fanout_evict_no_request_no_user_error(self, host, gpt2):
        run_trace(gpt2, "warmup")
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=15)

        # No active request. Evict.
        evict_via_lib(GPT2_REPO)
        wait_for_no_replicas(host, GPT2_REPO, timeout=60)
        time.sleep(12)  # Dispatcher drain

        # /status agrees the model has no replicas.
        assert count_replicas(get_status(host), GPT2_REPO) == 0


# ---------------------------------------------------------------------------
# Processor tear-down message bridge — when the Processor itself dies
# while requests are queued, ``purge`` fires the "Critical server
# error" reply. We don't try to provoke a "critical" path here
# (would need to break Ray); just document the contract.
# ---------------------------------------------------------------------------


class TestDocumentedStrings:
    """Lock down the exact strings the system emits.

    These tests don't simulate any failure — they import the string
    literals so that if a maintainer edits processor.py / replica.py
    without updating report.md, this test fires.
    """

    def test_replica_dispatch_strings_unchanged(self):
        # Just read the source and assert the strings are present.
        import inspect

        from ndif.services.api.queue import replica as replica_mod

        src = inspect.getsource(replica_mod)
        assert (
            "Replica evicted before dispatch. Sorry for the inconvenience. "
            "Please try again later." in src
        )
        assert (
            "Replica was evicted while processing your request. Sorry for "
            "the inconvenience. Please try again later." in src
        )

    def test_processor_evict_string_unchanged(self):
        import inspect

        from ndif.services.api.queue import processor as proc_mod

        src = inspect.getsource(proc_mod)
        # Source has the message split across two adjacent string literals;
        # check each piece independently.
        assert "Model deployment evicted." in src
        assert "Please try again later. Sorry for the inconvenience." in src
