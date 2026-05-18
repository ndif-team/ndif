"""Multi-replica behavior.

Verifies the replica pool primitives the controller exposes:

- ``deploy --replicas N`` places N replicas on first call, adds more on
  subsequent calls (deploy is *additive*).
- Each replica has its own actor with a unique ``replica_id``.
- Per-replica evict removes only the targeted replica, leaving siblings
  HOT.
- Per-replica restart kills one actor and the controller respawns it
  with the *same* replica_id (slot is preserved).
- Concurrent requests fan out across siblings — the Processor's queue
  feeds whichever Replica is free, so two parallel traces under one
  ``model_key`` complete faster than two serial traces would on a
  single-replica deployment.

Run with:
    pytest tests/report/test_replicas.py --run-remote -v
"""

import threading
import time

import pytest

from tests.report._helpers import (
    GPT2_REPO,
    QWEN_05B_REPO,
    count_replicas,
    deploy_via_lib,
    evict_all_models,
    evict_via_lib,
    find_replicas,
    get_status,
    model_key_from_repo,
    run_trace_with_sleep,
    wait_for_no_replicas,
    wait_for_replica_count,
)


@pytest.fixture(autouse=True)
def _reset(host):
    """Start each test with a clean cluster."""
    evict_all_models()
    time.sleep(12)


# ---------------------------------------------------------------------------
# deploy --replicas N
# ---------------------------------------------------------------------------


class TestDeployMultipleReplicas:

    def test_deploy_two_replicas_places_two(self, host):
        result = deploy_via_lib(GPT2_REPO, replicas=2)
        entry = result["deployments"][0]
        assert entry["error"] is None, entry
        assert len(entry["replicas"]) == 2, entry
        # Both unique
        assert len(set(entry["replicas"])) == 2, entry

        # /status agrees: 2 HOT replicas.
        wait_for_replica_count(host, GPT2_REPO, 2, level="HOT", timeout=60)
        assert count_replicas(get_status(host), GPT2_REPO, level="HOT") == 2

    def test_deploy_is_additive(self, host):
        """A second ``deploy --replicas 1`` adds a replica rather than
        replacing the existing one."""
        r1 = deploy_via_lib(GPT2_REPO, replicas=1)
        assert r1["deployments"][0]["error"] is None
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=60)
        rid_first = r1["deployments"][0]["replicas"][0]

        r2 = deploy_via_lib(GPT2_REPO, replicas=1)
        assert r2["deployments"][0]["error"] is None
        wait_for_replica_count(host, GPT2_REPO, 2, level="HOT", timeout=60)
        # The new replica must be a different replica_id.
        rid_second = r2["deployments"][0]["replicas"][0]
        assert rid_first != rid_second

        all_rids = {
            r["replica_id"] for r in find_replicas(get_status(host), GPT2_REPO)
        }
        assert rid_first in all_rids and rid_second in all_rids


# ---------------------------------------------------------------------------
# replica_id uniqueness and stability
# ---------------------------------------------------------------------------


class TestReplicaIdentity:

    def test_replica_ids_unique_per_replica(self, host):
        deploy_via_lib(GPT2_REPO, replicas=3)
        wait_for_replica_count(host, GPT2_REPO, 3, level="HOT", timeout=90)

        rids = [
            r["replica_id"] for r in find_replicas(get_status(host), GPT2_REPO)
        ]
        assert len(rids) == 3
        assert len(set(rids)) == 3, f"replica_ids must be unique; got {rids}"

    def test_actor_name_includes_replica_id(self, host):
        """The Ray actor name is ``{replica_id}:ModelActor:{model_key}``.

        Verifies by looking up each replica's actor by the documented
        name in the NDIF namespace.
        """
        from ndif.common.providers.ray import get_model_actor_handle

        deploy_via_lib(GPT2_REPO, replicas=2)
        wait_for_replica_count(host, GPT2_REPO, 2, level="HOT", timeout=90)

        # Give the controller a beat to register the named actor in Ray's
        # actor table — /status updates a touch before the gcs lookup
        # path is consistent.
        time.sleep(3)

        mk = model_key_from_repo(GPT2_REPO)
        replicas = find_replicas(get_status(host), GPT2_REPO)
        for r in replicas:
            rid = r["replica_id"]
            # get_model_actor_handle raises if the named actor isn't found.
            handle = get_model_actor_handle(mk, rid)
            assert handle is not None, (
                f"expected to find an actor named {rid}:ModelActor:{mk}"
            )


# ---------------------------------------------------------------------------
# Per-replica evict — only the targeted one goes
# ---------------------------------------------------------------------------


class TestPerReplicaEvict:

    def test_evict_one_leaves_siblings(self, host):
        deploy_via_lib(GPT2_REPO, replicas=2)
        wait_for_replica_count(host, GPT2_REPO, 2, level="HOT", timeout=60)

        replicas = find_replicas(get_status(host), GPT2_REPO)
        rid_to_evict = replicas[0]["replica_id"]
        rid_survivor = replicas[1]["replica_id"]

        evict_via_lib(GPT2_REPO, replica=rid_to_evict)

        # The evicted one may demote to WARM (CPU cache) rather than vanish;
        # what matters is that the *survivor* is still HOT.
        # Give the controller a moment to apply the delta.
        deadline = time.time() + 30
        while time.time() < deadline:
            status = get_status(host)
            current = find_replicas(status, GPT2_REPO)
            survivor_entry = next(
                (r for r in current if r["replica_id"] == rid_survivor), None
            )
            if (
                survivor_entry is not None
                and survivor_entry["deployment_level"] == "HOT"
            ):
                break
            time.sleep(1)

        status = get_status(host)
        levels_by_rid = {
            r["replica_id"]: r["deployment_level"]
            for r in find_replicas(status, GPT2_REPO)
        }
        assert levels_by_rid.get(rid_survivor) == "HOT", (
            f"survivor should still be HOT; got {levels_by_rid}"
        )
        # The targeted one is either WARM (demoted) or absent — both are
        # acceptable; what we never want is it still being HOT.
        assert levels_by_rid.get(rid_to_evict) != "HOT", (
            f"evicted replica should not still be HOT; got {levels_by_rid}"
        )


# ---------------------------------------------------------------------------
# Per-replica restart
# ---------------------------------------------------------------------------


class TestPerReplicaRestart:

    def test_restart_preserves_replica_id(self, host):
        """The Replica object holds the slot; killing the actor just makes
        the controller respawn under the same replica_id."""
        from ndif.cli.lib.restart import restart as _restart

        deploy_via_lib(GPT2_REPO, replicas=1)
        wait_for_replica_count(host, GPT2_REPO, 1, level="HOT", timeout=60)

        before = find_replicas(get_status(host), GPT2_REPO)
        assert len(before) == 1
        rid = before[0]["replica_id"]

        result = _restart(checkpoint=GPT2_REPO, replica=rid, timeout=120)
        assert result["replicas"], result
        per = result["replicas"][0]
        assert per["status"] == "restarted", per

        # After restart, the replica_id is preserved.
        after = find_replicas(get_status(host), GPT2_REPO)
        rids_after = {r["replica_id"] for r in after}
        assert rid in rids_after, (
            f"restart should preserve replica_id; was {rid}, now {rids_after}"
        )

    def test_restart_only_affects_target(self, host):
        """Restarting one replica leaves siblings untouched.

        We assert that the surviving sibling's actor handle resolves
        both before and after the restart — restart didn't bounce it.
        """
        from ndif.common.providers.ray import get_model_actor_handle

        deploy_via_lib(GPT2_REPO, replicas=2)
        wait_for_replica_count(host, GPT2_REPO, 2, level="HOT", timeout=90)
        time.sleep(3)  # let gcs settle

        replicas = find_replicas(get_status(host), GPT2_REPO)
        rid_a = replicas[0]["replica_id"]
        rid_b = replicas[1]["replica_id"]
        mk = model_key_from_repo(GPT2_REPO)

        # Sibling B resolves before the restart.
        actor_b_before = get_model_actor_handle(mk, rid_b)
        assert actor_b_before is not None

        from ndif.cli.lib.restart import restart as _restart

        _restart(checkpoint=GPT2_REPO, replica=rid_a, timeout=120)

        # B's handle still resolves by the same name in the NDIF namespace.
        actor_b_after = get_model_actor_handle(mk, rid_b)
        assert actor_b_after is not None

        # A also resolves — restart preserved its replica_id.
        actor_a_after = get_model_actor_handle(mk, rid_a)
        assert actor_a_after is not None


# ---------------------------------------------------------------------------
# Concurrent serving — siblings share the Processor queue
# ---------------------------------------------------------------------------


class TestConcurrentServing:
    """Two replicas should serve two concurrent traces in parallel.

    Approach: deploy 2 replicas, fire two sleep-3s traces concurrently.
    With one replica they'd serialize and take ~6s wall; with two they
    overlap and take ~3-4s. We assert "less than 5s" — wide enough to
    absorb dispatch + result-upload overhead, narrow enough to catch a
    regression that serializes everything onto one Replica.
    """

    def test_two_replicas_serve_concurrently(self, host, gpt2):
        deploy_via_lib(GPT2_REPO, replicas=2)
        wait_for_replica_count(host, GPT2_REPO, 2, level="HOT", timeout=90)

        results = {}

        def worker(name):
            try:
                run_trace_with_sleep(gpt2, sleep_s=5, prompt=f"{name} hi")
                results[name] = "ok"
            except Exception as e:
                results[name] = e

        t0 = time.time()
        threads = [
            threading.Thread(target=worker, args=("a",), daemon=True),
            threading.Thread(target=worker, args=("b",), daemon=True),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        elapsed = time.time() - t0

        assert results.get("a") == "ok", results
        assert results.get("b") == "ok", results
        # Sequential floor is ~10s (5s sleep × 2 + dispatch overhead);
        # parallel should land comfortably under 8s. Wide gap so a slow
        # CI machine doesn't false-fail, narrow enough to catch a
        # regression that serializes onto a single Replica.
        assert elapsed < 8.0, (
            f"two replicas should serve concurrently; took {elapsed:.1f}s "
            f"(sequential floor ~10s)"
        )


# ---------------------------------------------------------------------------
# Fan-out evict on a multi-replica deployment
# ---------------------------------------------------------------------------


class TestFanoutEvict:

    def test_fanout_evict_removes_all_replicas(self, host):
        deploy_via_lib(GPT2_REPO, replicas=3)
        wait_for_replica_count(host, GPT2_REPO, 3, level="HOT", timeout=120)

        evict_via_lib(GPT2_REPO)  # no replica= → fan-out
        wait_for_no_replicas(host, GPT2_REPO, timeout=60)
        assert count_replicas(get_status(host), GPT2_REPO) == 0


# ---------------------------------------------------------------------------
# Pinned multi-replica
# ---------------------------------------------------------------------------


class TestPinnedReplicas:

    def test_deploy_pinned_replicas(self, host):
        result = deploy_via_lib(QWEN_05B_REPO, replicas=2, pinned=True)
        entry = result["deployments"][0]
        assert entry["error"] is None, entry
        assert len(entry["replicas"]) == 2

        wait_for_replica_count(host, QWEN_05B_REPO, 2, level="HOT", timeout=120)
        replicas = find_replicas(get_status(host), QWEN_05B_REPO)
        pinned = [r for r in replicas if r.get("pinned")]
        assert len(pinned) == 2, (
            f"both replicas of a pinned deploy should be pinned; got "
            f"{[(r['replica_id'], r.get('pinned')) for r in replicas]}"
        )
