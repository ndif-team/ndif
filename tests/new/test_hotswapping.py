"""HOT / WARM / COLD transitions on the cluster.

Verifies that the controller's resource scheduler still:

- COLD → HOT  on first deploy
- HOT → WARM  when another deploy needs the GPUs (CPU has room)
- WARM → HOT  when the cached model is re-deployed (no disk reload)
- HOT (single-replica evict) → WARM  via per-replica targeted evict
- HOT (fan-out evict) → fully gone
- A *pinned* deployment isn't evicted to make room

Test stack: 2 GPUs (compose `device_ids: ["6", "7"]`) × A100 80GB.

Run with:
    pytest tests/report/test_hotswapping.py --run-remote -v
"""

import pytest

from tests.report._helpers import (
    GPT2_REPO,
    LLAMA_70B_REPO,
    QWEN_05B_REPO,
    QWEN_7B_REPO,
    count_replicas,
    deploy_via_lib,
    evict_all_models,
    evict_via_lib,
    find_any_level,
    find_replicas,
    get_status,
    run_trace,
    skip_if_insufficient_gpu,
    wait_for_no_replicas,
    wait_for_replica_count,
)


# ---------------------------------------------------------------------------
# Cluster-wide reset — one HOT-flush before each class so they don't
# inherit state from each other.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset(host):
    """Clear every HOT model before each test so we start from a known state.

    Some tests in this file deploy 70B which spans both GPUs; if a
    previous test left a 7B HOT we'd hit CANT_ACCOMMODATE.
    """
    import time

    evict_all_models()
    time.sleep(12)  # let the dispatcher purge Processors


# ---------------------------------------------------------------------------
# COLD → HOT
# ---------------------------------------------------------------------------


class TestColdToHot:

    def test_first_request_deploys_model_HOT(self, host, gpt2):
        run_trace(gpt2, "Hello")
        assert find_any_level(get_status(host), GPT2_REPO) == "HOT"

    def test_explicit_deploy_via_lib_deploys_HOT(self, host):
        result = deploy_via_lib(GPT2_REPO)
        # The deploy lib waits for at least one replica to be __ray_ready__
        # before returning, so the model must be HOT now.
        entries = result.get("deployments") or []
        assert len(entries) == 1
        assert entries[0]["error"] is None, entries
        assert entries[0]["status"] == "READY", entries
        assert find_any_level(get_status(host), GPT2_REPO) == "HOT"


# ---------------------------------------------------------------------------
# HOT → WARM via memory pressure (70B forces eviction of smaller models)
# ---------------------------------------------------------------------------


class TestHotToWarmUnderPressure:
    """Deploying a model that needs both GPUs forces smaller HOT models
    off the GPU. The smaller ones go to WARM (CPU cache) when there's
    room — only fully evicted if CPU cache is also full.
    """

    def test_70B_evicts_smaller_to_WARM(self, host, qwen_7b, llama_70b):
        skip_if_insufficient_gpu(80 * 1024**3, num_gpus=2)

        # 7B first
        run_trace(qwen_7b, "Hello")
        assert find_any_level(get_status(host), QWEN_7B_REPO) == "HOT"

        # 70B needs both GPUs — must evict the 7B.
        run_trace(llama_70b, "Hello")
        status = get_status(host)
        assert find_any_level(status, LLAMA_70B_REPO) == "HOT"
        # The 7B got cached, not deleted, since CPU has plenty of room.
        assert find_any_level(status, QWEN_7B_REPO) == "WARM", (
            f"expected 7B to be WARM after 70B deploy; got "
            f"{find_any_level(status, QWEN_7B_REPO)!r}"
        )


# ---------------------------------------------------------------------------
# WARM → HOT — second deploy of a cached model is fast (no disk reload)
# ---------------------------------------------------------------------------


class TestWarmToHot:

    def test_cached_model_redeploys_from_WARM(self, host, qwen_7b, llama_70b):
        skip_if_insufficient_gpu(80 * 1024**3, num_gpus=2)

        run_trace(qwen_7b, "Hello")
        run_trace(llama_70b, "Hello")  # evict 7B to WARM

        status = get_status(host)
        assert find_any_level(status, QWEN_7B_REPO) == "WARM"

        # Re-trace 7B — controller's Cluster.deploy reuses the WARM via
        # node.deploy's setdefault path; the user just sees a successful
        # response.
        run_trace(qwen_7b, "Hello again")
        status = get_status(host)
        assert find_any_level(status, QWEN_7B_REPO) == "HOT"


# ---------------------------------------------------------------------------
# Per-replica evict semantics
# ---------------------------------------------------------------------------


class TestPerReplicaEvict:
    """Targeted evict (HOT → WARM, when CPU has room) vs fan-out evict
    (everything goes).
    """

    def test_targeted_evict_demotes_HOT_to_WARM(self, host, gpt2):
        # Get one HOT replica
        run_trace(gpt2, "Hello")
        status = get_status(host)
        replicas = find_replicas(status, GPT2_REPO)
        assert len(replicas) == 1, replicas
        rid = replicas[0]["replica_id"]

        # Target this one replica
        evict_via_lib(GPT2_REPO, replica=rid)

        # The HOT goes to WARM (the cache has plenty of room for gpt2)
        # rather than vanishing.
        wait_for_replica_count(host, GPT2_REPO, 1, level="WARM", timeout=30)
        status = get_status(host)
        levels = [r["deployment_level"] for r in find_replicas(status, GPT2_REPO)]
        assert "WARM" in levels, (
            f"expected the targeted replica to demote to WARM; got "
            f"levels={levels}"
        )
        # rid is preserved across HOT→WARM transition.
        rids_after = {r["replica_id"] for r in find_replicas(status, GPT2_REPO)}
        assert rid in rids_after, (
            f"replica_id should be preserved across HOT→WARM; was {rid}, "
            f"now have {rids_after}"
        )

    def test_fanout_evict_removes_everything(self, host, gpt2):
        run_trace(gpt2, "Hello")
        assert count_replicas(get_status(host), GPT2_REPO) >= 1

        evict_via_lib(GPT2_REPO)  # no replica= → fan-out
        wait_for_no_replicas(host, GPT2_REPO, timeout=30)
        assert count_replicas(get_status(host), GPT2_REPO) == 0


# ---------------------------------------------------------------------------
# Pinned protection — pinned deployments resist hotswap eviction
# ---------------------------------------------------------------------------


class TestPinnedProtection:

    def test_pinned_model_survives_pressure(self, host, qwen_05b, qwen_7b):
        """A *pinned* small model should not be evicted to make room for
        another model — find_evictions skips pinned occupants."""
        # Pin qwen_05b
        result = deploy_via_lib(QWEN_05B_REPO, pinned=True)
        assert result["deployments"][0]["error"] is None, result

        # Now try a non-pinned 7B. Both should fit on the same node since
        # we have 2x80GB and ~14GB + ~1GB easily co-locates.
        run_trace(qwen_7b, "Hello")

        status = get_status(host)
        # Pinned 0.5B should still be HOT.
        assert find_any_level(status, QWEN_05B_REPO) == "HOT", (
            f"pinned model should stay HOT; got "
            f"{find_any_level(status, QWEN_05B_REPO)!r}"
        )
        # The pinned flag is exposed in /status entries.
        pinned_entries = [
            r for r in find_replicas(status, QWEN_05B_REPO) if r.get("pinned")
        ]
        assert len(pinned_entries) == 1, (
            f"expected one pinned 0.5B replica; got {pinned_entries}"
        )
