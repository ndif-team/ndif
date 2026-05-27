"""Dispatcher / Processor robustness — drift recovery and reconcile events.

Two failure modes the system protects against:

1. **Drift** — the controller's view and the dispatcher's view of which
   replicas exist disagree. Most common cause: an admin issued ``evict``
   directly against the controller and the dispatcher missed the event
   (or the event raced ahead of the dispatch). The Replica's
   ``dispatch`` path catches "Failed to look up actor" and self-destructs;
   the worker loop exits, ``on_replica_exit`` runs, and if it was the
   last replica the Processor aborts with the documented eviction string.

2. **Reconcile events** — to avoid waiting for drift to be discovered
   lazily, CLI deploy/evict and the dashboard publish a
   ``reconcile_model`` event on the ``dispatcher:events`` Redis stream.
   The dispatcher routes the event to the matching Processor which
   diffs its replica pool against ``Controller.get_deployment`` and
   adjusts. This test confirms the reconcile path actually fires by
   triggering it directly and watching the Processor follow.

Run with:
    pytest tests/report/test_dispatcher_robustness.py --run-remote -v
"""

import time

import pytest

from tests.report._helpers import (
    GPT2_REPO,
    count_replicas,
    deploy_via_lib,
    evict_all_models,
    evict_via_lib,
    find_replicas,
    get_status,
    model_key_from_repo,
    run_trace,
    wait_for_no_replicas,
    wait_for_replica_count,
)


@pytest.fixture(autouse=True)
def _reset(host):
    evict_all_models()
    time.sleep(12)


# ---------------------------------------------------------------------------
# Lazy Processor — Processor created on first request, not at startup
# ---------------------------------------------------------------------------


class TestLazyProcessorCreation:
    """The dispatcher does not pre-create Processors. The first request
    for a fresh model_key spawns the Processor lazily, which then drives
    PROVISIONING → DEPLOYING → READY.
    """

    def test_first_request_creates_processor(self, gpt2):
        # Pre-condition: no Processor exists. Verifiable indirectly via
        # the cold-start sequence (would error out on enqueue if the
        # Processor were already cancelled / missing).
        from tests.report._helpers import capture_responses

        with capture_responses() as recorded:
            run_trace(gpt2, "first")
        statuses = [s for s, _ in recorded]
        assert statuses[-1] == "COMPLETED", statuses


# ---------------------------------------------------------------------------
# Reconcile event — explicit CLI deploy → dispatcher sees it
# ---------------------------------------------------------------------------


class TestReconcileEventDelivered:
    """A controller-side state change (via the CLI's deploy/evict lib)
    publishes a ``reconcile_model`` event. The dispatcher receives it,
    routes it to the matching Processor, and the Processor diffs the
    pool. Easiest to observe: the Processor's autoscaling decisions
    track the new replica count.
    """

    def test_deploy_then_evict_observed_by_dispatcher(self, host, gpt2):
        # Spawn a Processor by issuing a request.
        run_trace(gpt2, "spawn processor")
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=60)

        # CLI-driven additive deploy: should add a replica AND publish a
        # reconcile_model event for GPT2_REPO. The Processor should pick
        # the new replica up.
        deploy_via_lib(GPT2_REPO, replicas=1)
        wait_for_replica_count(host, GPT2_REPO, 2, level="HOT", timeout=60)
        assert count_replicas(get_status(host), GPT2_REPO, level="HOT") == 2

        # Per-replica evict — reconcile event again, Processor should drop.
        replicas = find_replicas(get_status(host), GPT2_REPO)
        rid_to_evict = replicas[0]["replica_id"]
        evict_via_lib(GPT2_REPO, replica=rid_to_evict)
        # The surviving replica must still be HOT — Processor should
        # have removed the demoted slot from its pool, not the survivor.
        deadline = time.time() + 30
        while time.time() < deadline:
            n_hot = count_replicas(get_status(host), GPT2_REPO, level="HOT")
            if n_hot == 1:
                break
            time.sleep(1)
        assert (
            count_replicas(get_status(host), GPT2_REPO, level="HOT") == 1
        ), "expected exactly one survivor after per-replica evict"


# ---------------------------------------------------------------------------
# Drift recovery — even *without* the reconcile event, the next dispatch
# discovers the missing actor and self-heals
# ---------------------------------------------------------------------------


class TestDriftRecovery:
    """Bypass the CLI's notify_reconcile path: kill the Ray actor
    directly via ``ray.kill(no_restart=True)``, then issue a request.
    The Replica's dispatch raises "Failed to look up actor", flips
    ``self.dropped``, exits the worker loop, on_replica_exit fires, and
    — being the last replica — the Processor aborts with the documented
    "Model deployment evicted" string.

    Because this path replaces ``notify_reconcile`` entirely, it's the
    canonical drift-detection scenario.
    """

    def test_killing_actor_directly_surfaces_eviction_string(self, host, gpt2):
        import ray

        from ndif.common.providers.ray import get_model_actor_handle
        from tests.report._helpers import capture_responses

        # Get HOT.
        run_trace(gpt2, "warmup")
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=60)

        # Identify the actor and kill it without going through the
        # controller. This is the worst-case drift: dispatcher still
        # thinks the Replica is alive.
        mk = model_key_from_repo(GPT2_REPO)
        replicas = find_replicas(get_status(host), GPT2_REPO)
        rid = replicas[0]["replica_id"]
        actor = get_model_actor_handle(mk, rid)
        ray.kill(actor, no_restart=True)

        # Next request: should drift-recover and error out, NOT hang.
        with capture_responses() as recorded:
            with pytest.raises(Exception):
                run_trace(gpt2, "drifted")

        statuses = [s for s, _ in recorded]
        assert statuses[-1] == "ERROR", (
            f"drifted request should error, not hang; got {statuses}"
        )
        # The description should be one of the documented eviction strings.
        descs = [d for s, d in recorded if s == "ERROR"]
        joined = " | ".join(descs)
        assert (
            "Model deployment evicted" in joined
            or "Replica was evicted" in joined
            or "Replica evicted before dispatch" in joined
            or "Error submitting request" in joined
        ), f"unexpected drift-error description: {joined!r}"


# ---------------------------------------------------------------------------
# Processor tear-down on last-replica exit
# ---------------------------------------------------------------------------


class TestLastReplicaTriggersProcessorTeardown:
    """When the last Replica exits (drift OR clean evict), the Processor
    aborts. A subsequent request to the same model_key cold-starts a
    fresh Processor — drift state from the previous Processor does NOT
    leak."""

    def test_subsequent_request_cold_starts_cleanly(self, host, gpt2):
        from tests.report._helpers import capture_responses

        # HOT
        run_trace(gpt2, "warmup")
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=60)

        # Evict + wait for tear-down + extra time for the Dispatcher's
        # 10s brpop to drain its eviction queue.
        evict_via_lib(GPT2_REPO)
        wait_for_no_replicas(host, GPT2_REPO, timeout=60)
        time.sleep(12)

        # Fresh request — full cold start, ends COMPLETED.
        with capture_responses() as recorded:
            run_trace(gpt2, "after tear-down")
        statuses = [s for s, _ in recorded]
        assert statuses[-1] == "COMPLETED", (
            f"after Processor tear-down, next request must cold-start "
            f"cleanly; got {statuses}"
        )


# ---------------------------------------------------------------------------
# Notify path is best-effort — broker outage shouldn't break deploy
# ---------------------------------------------------------------------------


class TestNotifyIsBestEffort:
    """``notify_reconcile`` swallows broker errors. We don't actually
    take Redis down here (would break the dispatcher). Instead, we
    confirm the docstring contract: ``notify_reconcile`` returns None
    even when the broker URL is junk.
    """

    def test_notify_reconcile_swallows_broker_errors(self):
        from ndif.cli.lib.util import notify_reconcile

        # Junk broker URL; must not raise.
        notify_reconcile("redis://127.0.0.1:1", ["fake-model-key"])
