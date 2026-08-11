"""Placement arithmetic: how many cards a model gets, and what it's charged.

Pure bookkeeping over a synthetic node — no server, no GPUs, no models — so this
runs anywhere, unlike the rest of the suite. Worth having precisely because the
numbers here are invisible at runtime: a wrong share doesn't fail, it quietly
stops a card being usable, and a wrong eviction leaves the controller believing
in a replica that isn't there.
"""

from __future__ import annotations

import math

import pytest

# Unlike the remote tests beside it, this one imports the server package. Node
# reaches Deployment reaches the model actor's base, which pulls in boto3 for the
# result store -- so a client-only environment (the venv a developer drives the
# remote suite from) can't collect this file at all, and a bare ImportError here
# aborts collection for the whole directory. Skip instead: the arithmetic below
# is worth running wherever it can be, and nowhere is it urgent.
pytest.importorskip(
    "boto3", reason="the server package's imports need the server's dependencies"
)

from ndif.services.ray.deployments.controller.cluster.evaluator import tp_degree
from ndif.services.ray.deployments.controller.cluster.node import (
    CPUResources,
    GPU,
    GPUResources,
    Node,
)

CARD = 85_899_345_920  # an A100-80GB
HOST_RAM = 2_000_000_000_000


def make_node(cards: int = 8) -> Node:
    return Node(
        "n",
        "node",
        GPUResources("A100", [GPU(index, CARD) for index in range(cards)]),
        CPUResources(HOST_RAM, HOST_RAM),
    )


class Parkable:
    CACHEABLE = True


class Fixed:
    """An actor whose replica can't be parked — a tensor-parallel group."""

    CACHEABLE = False


class TestPerGpuCharge:
    """A multi-GPU model is charged its share of each card, not all of it."""

    def test_a_single_card_model_is_charged_its_whole_size(self):
        node = make_node()
        candidate = node.evaluate("m", 40_000_000_000)

        assert list(candidate.gpus.values()) == [40_000_000_000]

    def test_a_multi_card_model_is_charged_an_even_share(self):
        node = make_node()
        size = 226_700_000_000  # spans three cards, so four under tp_degree

        candidate = node.evaluate("m", size, max_tp=8)

        assert len(candidate.gpus) == 4
        assert set(candidate.gpus.values()) == {math.ceil(size / 4)}

    def test_the_cards_it_spans_stay_usable(self):
        # The regression this exists for: charging 100% of every card a model
        # touched meant a replica using a third of four cards blocked all four.
        node = make_node()
        size = 226_700_000_000
        candidate = node.evaluate("m", size, max_tp=8)

        node.deploy("m", candidate, size, actor_class="x")

        spanned = [gpu for gpu in node.gpu_resources.gpus if gpu.index in candidate.gpus]
        assert all(gpu.available_memory_bytes > 0 for gpu in spanned)

    def test_a_second_model_fits_alongside_the_first(self):
        node = make_node(cards=4)
        size = 226_700_000_000
        first = node.evaluate("a", size, max_tp=8)
        node.deploy("a", first, size, actor_class="x")

        second = node.evaluate("b", 20_000_000_000)

        assert second.gpus, "nothing fit, though every card had room"


class TestTensorParallelRounding:
    """A model that will be split has to be split evenly."""

    def test_the_count_rises_to_a_workable_degree(self):
        node = make_node()
        # Spans three cards; 3 doesn't divide 8, so it takes four.
        size = 226_700_000_000

        assert len(node.evaluate("m", size, max_tp=8).gpus) == 4

    def test_a_model_that_cannot_be_split_keeps_its_count(self):
        node = make_node()
        size = 226_700_000_000

        assert len(node.evaluate("m", size, max_tp=None).gpus) == 3

    def test_a_degree_that_cannot_reach_the_count_keeps_it(self):
        # Qwen2.5-0.5B's shape: capped at 2 by its key/value heads. Needing
        # three cards, it has to be spread some other way.
        node = make_node()
        size = 226_700_000_000

        assert len(node.evaluate("m", size, max_tp=2).gpus) == 3

    @pytest.mark.parametrize(
        "limit,gpus,expected",
        [
            (8, 1, None),   # one card: not parallel at all
            (8, 2, 2),      # exact divisor
            (8, 3, 4),      # 3 doesn't divide 8
            (8, 5, 8),      # nor do 5, 6, 7
            (8, 9, None),   # more cards than it shards into
            (2, 3, None),   # capped below what it needs
            (None, 4, None),  # no sharding plan
        ],
    )
    def test_degree_selection(self, limit, gpus, expected):
        assert tp_degree(limit, gpus) == expected


class TestEviction:
    """A replica whose actor can't be parked is removed, not cached."""

    def test_a_parkable_replica_is_demoted_to_warm(self):
        node = make_node()
        size = 40_000_000_000
        candidate = node.evaluate("m", size)
        node.deploy("m", candidate, size, actor_class=Parkable)

        node.evict("m", next(iter(node.deployments["m"])))

        assert node.cache.get("m"), "a parkable replica should keep its weights on CPU"

    def test_a_fixed_replica_is_removed_outright(self):
        # Parking a tensor-parallel group cannot work — every rank's device is
        # fixed when its process starts — and demoting it anyway would leave the
        # controller believing in a WARM replica while the actor holds its GPUs.
        node = make_node()
        size = 40_000_000_000
        candidate = node.evaluate("m", size)
        node.deploy("m", candidate, size, actor_class=Fixed)

        node.evict("m", next(iter(node.deployments["m"])))

        assert not node.cache.get("m")

    @pytest.mark.parametrize("actor", [Parkable, Fixed])
    def test_either_way_the_gpus_come_back(self, actor):
        node = make_node()
        size = 40_000_000_000
        candidate = node.evaluate("m", size)
        node.deploy("m", candidate, size, actor_class=actor)

        node.evict("m", next(iter(node.deployments["m"])))

        assert all(
            gpu.available_memory_bytes == CARD for gpu in node.gpu_resources.gpus
        )


class TestCacheEntryContract:
    """The evaluator's cache entry carries everything the status reads off it.

    The controller builds its status straight from these attributes, so one gone
    missing is an AttributeError inside a Ray actor — surfacing as "Could not
    retrieve cluster status" with the real cause a log dive away. That is how
    `revision` was lost once already.
    """

    def test_every_attribute_the_controller_reads_exists(self):
        import re
        from pathlib import Path

        from ndif.services.ray.deployments.controller.cluster.evaluator import CacheEntry

        controller = (
            Path(__file__).resolve().parents[1]
            / "src/ndif/services/ray/deployments/controller/controller.py"
        )
        # Whatever the status path reads as `entry.<name>` has to be a real field.
        wanted = set(re.findall(r"\bentry\.([a-z_]+)", controller.read_text()))
        wanted.discard("get")  # a dict elsewhere in the same file

        entry = CacheEntry(
            base_size_in_bytes=1,
            n_params=1,
            config=None,
            revision=None,
            dtype="bfloat16",
            trust_remote_code=False,
            max_tp=None,
        )
        missing = [name for name in sorted(wanted) if not hasattr(entry, name)]
        assert not missing, f"the controller reads entry.{missing} but CacheEntry has no such field"


class TestShardSettleVerdict:
    """When a failed request costs the replica, and when it doesn't.

    Every rank runs the user's block, so a block that raises raises on all of
    them. Reading that as a wedged group restarted a healthy multi-GPU replica
    on every user-code bug — and the person who paid was whoever sent the *next*
    request, which failed to submit while the actor reloaded.
    """

    @staticmethod
    def _deployment(raised, lost, rank_zero_raised):
        from ndif.services.ray.tp.model import TPModelDeployment

        class Group:
            healthy = True

            def collect(self, timeout):
                return list(raised), list(lost)

            def stop(self):
                # The deployment's __del__ tears its group down; without this
                # the collection raises during GC and pytest reports it as an
                # unraisable exception on some unrelated test.
                pass

        deployment = TPModelDeployment.__new__(TPModelDeployment)
        deployment.model_key = "m"
        deployment.group = Group()
        deployment.rank_zero_raised = rank_zero_raised
        # These all describe a request that reached the forward; a request that
        # never ran has nothing to collect and is covered separately.
        deployment.committed = True
        deployment.dispatched = True
        deployment.stood_down = True
        return deployment

    def test_the_group_survives_an_error_every_rank_hit(self):
        deployment = self._deployment(
            raised=["rank 1: ValueError", "rank 2: ValueError"],
            lost=[],
            rank_zero_raised=True,
        )
        assert deployment._await_shards() is True

    def test_a_shard_failing_alone_means_the_ranks_diverged(self):
        # Rank 0 came through fine, so this shard left the collective stream
        # somewhere the others did not. Unrecoverable -- restart.
        deployment = self._deployment(
            raised=["rank 2: CUDA error"], lost=[], rank_zero_raised=False
        )
        assert deployment._await_shards() is False

    def test_a_shard_that_never_answered_is_not_settled(self):
        # True whatever rank 0 did: a silent shard is in no known state.
        for rank_zero_raised in (True, False):
            deployment = self._deployment(
                raised=[], lost=["rank 3: timed out"], rank_zero_raised=rank_zero_raised
            )
            assert deployment._await_shards() is False

    def test_a_clean_run_is_settled(self):
        deployment = self._deployment(raised=[], lost=[], rank_zero_raised=False)
        assert deployment._await_shards() is True


class TestStandDownProtocol:
    """A request that never ran must cost the request, not the replica.

    The two-phase protocol exists so a payload that fails to deserialize is
    reported *before* any rank starts a forward. It didn't deliver that: the
    shards that had already prepared were left parked, and `cleanup` then asked
    them for a completion they were never going to send -- so the group timed out
    and the replica restarted. Every version of "this request could not be built"
    cost a multi-GPU reload.

    These drive the real `execute`/`_await_shards` against a scripted group, so
    they cover the state machine rather than the verdict function that reads it.
    """

    @staticmethod
    def _deployment(group):
        from ndif.services.ray.tp.model import TPModelDeployment

        deployment = TPModelDeployment.__new__(TPModelDeployment)
        deployment.model_key = "m"
        deployment.group = group
        deployment.committed = False
        deployment.dispatched = False
        deployment.rank_zero_raised = False
        deployment.stood_down = True
        deployment.abort = _NullAbort()
        return deployment

    class Group:
        """Records what rank 0 asked of the shards."""

        healthy = True

        def __init__(self, prepare_raises=None, release_lost=()):
            self.prepare_raises = prepare_raises
            self.release_lost = list(release_lost)
            self.calls = []

        def prepare(self, *args):
            self.calls.append("prepare")
            if self.prepare_raises is not None:
                raise self.prepare_raises

        def go(self):
            self.calls.append("go")

        def release(self, *args, **kwargs):
            self.calls.append("release")
            return list(self.release_lost)

        def collect(self, timeout):
            self.calls.append("collect")
            return [], []

        def stop(self):
            pass

    def test_a_shard_that_cannot_deserialize_stands_the_others_down(self):
        # The regression: `prepare` sat outside the try, so a ShardError escaped
        # without releasing the shards that had already prepared.
        from ndif.services.ray.tp.host import ShardError

        group = self.Group(prepare_raises=ShardError("rank 2: bad payload"))
        deployment = self._deployment(group)

        with pytest.raises(ShardError):
            deployment.execute(_request())

        assert "release" in group.calls, "the prepared shards were left parked"

    def test_a_request_that_never_ran_is_not_collected_from(self):
        # Released shards go back to idle and send no DONE. Asking for one timed
        # out and restarted the replica.
        group = self.Group()
        deployment = self._deployment(group)
        deployment.dispatched = True
        deployment.committed = False
        deployment.group.release()
        group.calls.clear()

        assert deployment._await_shards() is True
        assert "collect" not in group.calls

    def test_a_shard_that_will_not_stand_down_does_cost_the_replica(self):
        # The one version of "didn't run" that is not survivable: a shard that
        # was told to stand down and never confirmed is in no known state.
        group = self.Group(release_lost=["rank 3: timed out"])
        deployment = self._deployment(group)
        deployment.dispatched = True

        deployment.execute_raised = None
        # Drive just the stand-down half of `execute`'s finally.
        lost = group.release()
        if lost:
            deployment.stood_down = False

        assert deployment._await_shards() is False

    def test_a_committed_request_is_still_collected_from(self):
        group = self.Group()
        deployment = self._deployment(group)
        deployment.dispatched = True
        deployment.committed = True

        assert deployment._await_shards() is True
        assert "collect" in group.calls


class _NullAbort:
    """`execute`'s finally disarms the abort checkpoint; nothing armed it here."""

    def disarm(self):
        pass


def _request():
    """The least a request needs to reach `prepare`."""

    class Request:
        payload = b""
        compress = False
        env = {}

    return Request()
