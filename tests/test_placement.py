"""Placement arithmetic: how many cards a model gets, and what it's charged.

Pure bookkeeping over a synthetic node — no server, no GPUs, no models — so this
runs anywhere, unlike the rest of the suite. Worth having precisely because the
numbers here are invisible at runtime: a wrong share doesn't fail, it quietly
stops a card being usable, and a wrong eviction leaves the controller believing
in a replica that isn't there.
"""

from __future__ import annotations

import math
import threading
import time

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
    def _deployment(raised, lost, rank_zero_raised, rank_zero_error=None):
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
        # What rank 0 hit, not just that it did: a transport failure means a shard
        # probably died and took the socket with it, which changes whose error is
        # worth reporting.
        deployment.rank_zero_error = rank_zero_error
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


class TestPlacementOverrides:
    """An operator can supply any part of the derivation and have the rest filled in.

    Everything about where a replica goes comes from one number -- the model's
    padded size -- so with only `padding_factor` exposed, "give this model four
    cards" had to be expressed as a fudge factor computed backwards against the
    cluster's card size. These name the thing wanted instead.
    """

    def test_an_explicit_gpu_count_is_taken(self):
        node = make_node()
        # A model that would otherwise fit on one card.
        candidate = node.evaluate("m", 10_000_000_000, gpus=4, max_tp=8)

        assert len(candidate.gpus) == 4

    def test_a_count_the_model_cannot_split_into_is_refused(self):
        # Better here than at load, after the cards are reserved and the weights
        # read: transformers will not run an uneven split at all.
        from ndif.services.ray.deployments.controller.cluster.node import CandidateLevel

        node = make_node()
        candidate = node.evaluate("m", 10_000_000_000, gpus=3, max_tp=8)

        assert candidate.candidate_level == CandidateLevel.CANT_ACCOMMODATE

    def test_the_refusal_says_why(self):
        # Not "the cluster ran out of room", which is what every other refusal
        # says and would send someone to look at the wrong thing entirely.
        node = make_node()
        candidate = node.evaluate("m", 10_000_000_000, gpus=3, max_tp=8)

        assert candidate.reason and "even split" in candidate.reason

    def test_an_ordinary_refusal_names_no_reason(self):
        # Leaving it None is what lets the caller fall back to the generic
        # message rather than inventing one.
        node = make_node(cards=1)
        candidate = node.evaluate("m", 10_000_000_000_000)

        assert candidate.reason is None

    def test_a_count_is_still_allowed_for_a_model_that_cannot_shard(self):
        # No sharding plan means accelerate spreads it layer-by-layer, and any
        # count works.
        node = make_node()
        candidate = node.evaluate("m", 10_000_000_000, gpus=3, max_tp=None)

        assert len(candidate.gpus) == 3

    def test_one_gpu_is_never_refused(self):
        node = make_node()
        assert len(node.evaluate("m", 10_000_000_000, gpus=1, max_tp=2).gpus) == 1

    def test_an_explicit_share_overrides_the_even_split(self):
        node = make_node()
        candidate = node.evaluate(
            "m", 40_000_000_000, gpus=2, per_gpu_bytes=30_000_000_000
        )

        assert set(candidate.gpus.values()) == {30_000_000_000}

    def test_without_an_override_the_count_is_still_derived(self):
        node = make_node()
        size = 226_700_000_000

        assert len(node.evaluate("m", size, max_tp=8).gpus) == 4


class TestEvaluatorOverrides:
    """Sizing inputs the caller can supply instead of having them worked out."""

    @staticmethod
    def _evaluator(**kwargs):
        from ndif.services.ray.deployments.controller.cluster.evaluator import (
            ModelEvaluator,
        )

        return ModelEvaluator(**kwargs)

    def test_a_given_size_skips_the_estimate(self, monkeypatch):
        from ndif.services.ray.deployments.controller.cluster import evaluator as mod

        evaluator = self._evaluator(padding_factor=0.0, padding_bias=0)
        monkeypatch.setattr(
            mod.ModelEvaluator,
            "_entry",
            lambda self, *a: (_ for _ in ()).throw(AssertionError("estimated anyway")),
        )

        # The description still runs and is allowed to fail; the size does not.
        assert evaluator("k", size_bytes=1_000) == 1_000

    def test_a_given_size_survives_an_unreachable_hub(self, monkeypatch):
        # The point of measuring it yourself: a deploy that names its own size
        # needs no network, where an estimated one cannot be placed at all.
        from ndif.services.ray.deployments.controller.cluster import evaluator as mod

        evaluator = self._evaluator(padding_factor=0.0, padding_bias=0)
        monkeypatch.setattr(
            mod.ModelEvaluator,
            "_entry",
            lambda self, *a: (_ for _ in ()).throw(RuntimeError("hub down")),
        )

        assert evaluator("k", size_bytes=2_000) == 2_000

    def test_an_estimated_size_still_fails_when_the_hub_is_down(self, monkeypatch):
        from ndif.services.ray.deployments.controller.cluster import evaluator as mod

        evaluator = self._evaluator()
        monkeypatch.setattr(
            mod.ModelEvaluator,
            "_entry",
            lambda self, *a: (_ for _ in ()).throw(RuntimeError("hub down")),
        )

        assert isinstance(evaluator("k"), Exception)

    def test_padding_bias_can_be_overridden_per_model(self, monkeypatch):
        from ndif.services.ray.deployments.controller.cluster import evaluator as mod

        evaluator = self._evaluator(padding_factor=0.0, padding_bias=500)
        monkeypatch.setattr(mod.ModelEvaluator, "_entry", lambda self, *a: None)

        assert evaluator("k", size_bytes=1_000) == 1_500
        assert evaluator("k", size_bytes=1_000, padding_bias=0) == 1_000

    def test_max_tp_can_be_supplied_when_the_checkpoint_cannot_be_read(self, monkeypatch):
        from ndif.services.ray.deployments.controller.cluster import evaluator as mod

        # Needs a cluster with tensor parallelism on; a disabled one answers None
        # whatever the config says (see TestTensorParallelIsOptIn).
        evaluator = self._evaluator(tp_model_actor_class="pkg.TPActor")
        monkeypatch.setattr(
            mod.ModelEvaluator,
            "_entry",
            lambda self, *a: (_ for _ in ()).throw(RuntimeError("hub down")),
        )

        assert evaluator.max_tp("k", override=8) == 8

    def test_zero_max_tp_means_never_tensor_parallel(self, monkeypatch):
        # Spelled as a number so a config can say it; None means "nobody said".
        from ndif.services.ray.deployments.controller.cluster import evaluator as mod

        evaluator = self._evaluator(tp_model_actor_class="pkg.TPActor")
        monkeypatch.setattr(mod.ModelEvaluator, "_entry", lambda self, *a: None)

        assert evaluator.max_tp("k", override=0) is None
        assert evaluator.actor_class("k", 4, "default.Actor", max_tp_override=0) == (
            "default.Actor"
        )


class TestTheTwoExecutorsAreOne:
    """The sandbox calls the shared executor rather than keeping its own copy.

    Structural only, and deliberately so: these catch the *shape* that let the
    two paths drift -- a second implementation of the block executor -- and they
    cannot tell you whether the numbers agree. That is
    `test_sandbox_conformance.py`, which measures it against a live server. Do
    not add assertions here that sound numeric; they would pass whether or not
    the two paths compute the same thing, which is exactly how the divergence
    went unnoticed the first time.
    """

    def test_the_runner_calls_the_shared_executor(self):
        import inspect

        from ndif.services.ray.sandbox import nns

        source = inspect.getsource(nns)
        assert "execute_traced_block" in source

    def test_the_runner_does_not_reimplement_the_trace_scope(self):
        # inc()/dec() around tracer.execute is exactly what it used to duplicate.
        import inspect

        from ndif.services.ray.sandbox import nns

        source = inspect.getsource(nns._run)
        assert "inc()" not in source and "dec()" not in source

    def test_the_runner_applies_the_source_cache_scope(self):
        import inspect

        from ndif.services.ray.sandbox import nns

        assert "block_scope" in inspect.getsource(nns.run)

    def test_the_host_brackets_the_forward_it_drives(self):
        # The half that is easy to miss: the runner brackets its own work, but
        # under the sandbox the model's *forward* runs in the host process,
        # driven over a socket, so it needs the region too. Whether that region
        # is the *right* one is measured in test_sandbox_conformance.py.
        import inspect

        from ndif.services.ray.sandbox import driver

        source = inspect.getsource(driver.SandboxDriver.interleave)
        assert "request_dtype" in source

    def test_no_execution_path_builds_its_own_region(self):
        # Not "there is exactly one torch.autocast in the tree" -- that breaks on
        # the first legitimate unrelated use. The property is that the three
        # places running a request all reach for the shared one.
        import inspect

        from ndif.services.ray.deployments.modeling import base, nns
        from ndif.services.ray.sandbox import model
        from ndif.services.ray.sandbox import nns as sandbox_nns

        for module in (sandbox_nns, model, base):
            source = inspect.getsource(module)
            assert "torch.autocast" not in source, (
                f"{module.__name__} builds its own autocast region instead of "
                "calling nns.request_dtype"
            )
        assert "torch.autocast" in inspect.getsource(nns.request_dtype)

    def test_the_dtype_reaches_the_runner(self):
        # It has no model to ask, so the host has to ship it -- without which
        # `execute_traced_block` would autocast to the wrong thing, or nothing.
        import inspect

        from ndif.services.ray.sandbox import model, runner

        assert "dtype" in inspect.signature(nns_run := __import__(
            "ndif.services.ray.sandbox.nns", fromlist=["run"]
        ).run).parameters
        assert "str(self.dtype)" in inspect.getsource(model.SandboxHost.run_in_runner)
        assert "dtype" in inspect.getsource(runner.Runner.handle)


class TestDtypeResolution:
    """The dtype crosses a socket and a command line as a string."""

    @pytest.mark.parametrize("given", ["bfloat16", "torch.bfloat16"])
    def test_both_spellings_resolve(self, given):
        import torch

        from ndif.services.ray.deployments.modeling.util import resolve_dtype

        assert resolve_dtype(given) is torch.bfloat16

    def test_a_torch_dtype_round_trips_through_str(self):
        # The trap this exists for: `str(torch.bfloat16)` is "torch.bfloat16",
        # which the obvious `getattr(torch, name)` cannot resolve.
        import torch

        from ndif.services.ray.deployments.modeling.util import resolve_dtype

        for dtype in (torch.bfloat16, torch.float16, torch.float32):
            assert resolve_dtype(str(dtype)) is dtype

    def test_nonsense_is_refused(self):
        from ndif.services.ray.deployments.modeling.util import resolve_dtype

        with pytest.raises(ValueError, match="Unknown torch dtype"):
            resolve_dtype("float3")


class TestDrainingTheGroup:
    """Reading the shards' replies must wait on all of them at once.

    Serializing with a shrinking per-shard timeout looks equivalent and is not:
    the ranks leave a collective together, so a budget spent on the first shard
    left the rest with a timeout of *zero* -- which is non-blocking mode, not "no
    wait", so they raised BlockingIOError and were reported lost. A lost shard
    restarts the replica, so a slow first reply cost four GPUs.

    These use real socket pairs, because the bug was in what a real socket does
    with `settimeout(0.0)` and no stub would have reproduced it.
    """

    @staticmethod
    def _group(count=3):
        import socket

        from ndif.services.ray.sandbox.protocol import encode, send_frame
        from ndif.services.ray.tp.host import Shard, ShardGroup

        group = ShardGroup(model_key="m", gpu_ids=list(range(count + 1)), dtype="bfloat16")
        ends = []
        for rank in range(1, count + 1):
            ours, theirs = socket.socketpair()
            shard = Shard(rank=rank, process=None, sock=ours)
            group.shards.append(shard)
            ends.append(theirs)

        def answer(index, value, after=0.0):
            def send():
                send_frame(ends[index], encode(value))

            if after:
                threading.Timer(after, send).start()
            else:
                send()

        return group, answer

    def test_a_slow_first_reply_does_not_lose_the_others(self):
        # The regression, exactly: rank 1 answers late enough to eat the budget,
        # and ranks 2 and 3 answered long ago.
        group, answer = self._group()
        answer(1, ("DONE",))
        answer(2, ("DONE",))
        answer(0, ("DONE",), after=0.3)

        raised, lost = group.collect(timeout=5.0)

        assert lost == [], f"live shards reported lost: {lost}"
        assert raised == []

    def test_a_shard_that_never_answers_is_lost(self):
        group, answer = self._group()
        answer(0, ("DONE",))
        answer(1, ("DONE",))

        raised, lost = group.collect(timeout=0.5)

        assert len(lost) == 1 and "rank 3" in lost[0]

    def test_an_error_is_raised_not_lost(self):
        group, answer = self._group()
        answer(0, ("ERROR", "boom"))
        answer(1, ("DONE",))
        answer(2, ("DONE",))

        raised, lost = group.collect(timeout=5.0)

        assert lost == []
        assert len(raised) == 1 and "boom" in raised[0]

    def test_release_accepts_acknowledgements_in_any_order(self):
        group, answer = self._group()
        answer(2, ("IDLE", 3))
        answer(0, ("IDLE", 1), after=0.2)
        answer(1, ("IDLE", 2))

        assert group.release(timeout=5.0) == []

    def test_release_reports_a_shard_that_will_not_stand_down(self):
        group, answer = self._group()
        answer(0, ("IDLE", 1))
        answer(1, ("DONE",))  # wrong message: not an acknowledgement
        answer(2, ("IDLE", 3))

        lost = group.release(timeout=1.0)

        assert len(lost) == 1 and "rank 2" in lost[0]

    def test_the_deadline_is_for_the_group_not_each_shard(self):
        # The bound the caller was promised: `cleanup` runs on the actor's event
        # loop, so `timeout * (tp - 1)` would stall every other call into it.
        group, answer = self._group(count=3)
        started = time.time()

        group.collect(timeout=0.5)

        assert time.time() - started < 1.5


class TestMaxTpOverrideIsACap:
    """A configured degree may narrow what the checkpoint supports, not widen it.

    Asking for more ways than the weights divide into passes every check on this
    path -- the degree looks workable, the GPU count looks even -- and then
    transformers refuses at load, with the cards already reserved and the weights
    read across them. Moving that refusal earlier is the whole point of the path.
    """

    @staticmethod
    def _evaluator(limit):
        from ndif.services.ray.deployments.controller.cluster import evaluator as mod

        # A cluster with tensor parallelism switched on -- capping a degree is
        # only a question for one that does (see TestTensorParallelIsOptIn).
        evaluator = mod.ModelEvaluator(tp_model_actor_class="pkg.TPActor")

        class Entry:
            max_tp = limit

        evaluator._entry = lambda *a, **k: Entry()
        return evaluator

    def test_an_override_above_the_real_limit_is_clamped(self):
        assert self._evaluator(limit=2).max_tp("k", override=8) == 2

    def test_an_override_below_it_is_taken(self):
        assert self._evaluator(limit=8).max_tp("k", override=2) == 2

    def test_zero_still_means_never(self):
        assert self._evaluator(limit=8).max_tp("k", override=0) is None

    def test_no_override_still_asks_the_checkpoint(self):
        assert self._evaluator(limit=8).max_tp("k") == 8

    def test_a_clamped_degree_no_longer_matches_the_gpu_count(self):
        # The end the clamp exists for: `gpus: 8` on a model that halves used to
        # produce max_tp=8, which made 8 a workable degree and reserved 8 cards.
        from ndif.services.ray.deployments.controller.cluster.node import CandidateLevel

        limit = self._evaluator(limit=2).max_tp("k", override=8)
        node = make_node()
        candidate = node.evaluate("m", 10_000_000_000, gpus=8, max_tp=limit)

        assert candidate.candidate_level == CandidateLevel.CANT_ACCOMMODATE

    def test_an_override_wins_when_the_checkpoint_says_nothing(self):
        # No plan to read: the operator's number is the only information there is,
        # and refusing it would make the field useless for exactly the models that
        # need it.
        assert self._evaluator(limit=None).max_tp("k", override=4) == 4


class TestFileEntriesSeeTheCliFlags:
    """`ndif deploy -f models.yaml --gpus 4` must not silently drop `--gpus`.

    Click accepted the flag and the file branch hardcoded None, so the deploy
    went out with the count the size implied and nothing said otherwise.
    """

    @staticmethod
    def _load(text, **defaults):
        import tempfile
        from pathlib import Path

        from ndif.cli.lib.model_config import load_model_config

        path = Path(tempfile.mkdtemp()) / "models.yaml"
        path.write_text(text)
        return load_model_config(path, **defaults)

    def test_a_flag_reaches_a_bare_checkpoint_entry(self):
        specs = self._load("models:\n  - gpt2\n", default_gpus=4, default_max_tp=8)

        assert specs[0]["gpus"] == 4 and specs[0]["max_tp"] == 8

    def test_a_flag_reaches_a_dict_entry(self):
        specs = self._load(
            "models:\n  - checkpoint: gpt2\n", default_size_bytes=123, default_padding_bias=7
        )

        assert specs[0]["size_bytes"] == 123 and specs[0]["padding_bias"] == 7

    def test_the_file_wins_over_the_flag(self):
        specs = self._load("models:\n  - checkpoint: gpt2\n    gpus: 2\n", default_gpus=4)

        assert specs[0]["gpus"] == 2

    def test_every_flag_with_a_default_is_wired_to_it(self):
        # The bug was one missing keyword argument. The invariant that catches
        # the whole class: if a CLI option has a matching `default_<name>` on
        # `load_model_config`, the command must pass it — otherwise the flag is
        # accepted and dropped for `-f` deploys.
        #
        # Not "every default is passed": `execution_timeout_seconds` and
        # `envoy_class` are settable in the file only, and have no flag.
        import inspect

        from ndif.cli.commands import deploy as command
        from ndif.cli.lib.model_config import load_model_config

        source = inspect.getsource(command.deploy.callback)
        defaults = {
            name
            for name in inspect.signature(load_model_config).parameters
            if name.startswith("default_")
        }
        flags = {
            parameter.name for parameter in command.deploy.params
        }

        unwired = [
            f"default_{flag}"
            for flag in flags
            if f"default_{flag}" in defaults and f"default_{flag}=" not in source
        ]
        assert not unwired, f"accepted as flags but never passed: {unwired}"


class TestTensorParallelIsOptIn:
    """Tensor parallelism is off unless a cluster names the actor that serves it.

    `NDIF_TP_MODEL_ACTOR_CLASS` unset means *off*, not "use the built-in one". A
    sharded replica cannot be cached, is not sandboxed, and needs a transformers
    new enough to shard correctly, so a cluster should get one because somebody
    asked for it -- not because it deployed a model that happened to be
    divisible.
    """

    @staticmethod
    def _evaluator(actor_class=None, limit=8):
        from ndif.services.ray.deployments.controller.cluster import evaluator as mod

        evaluator = mod.ModelEvaluator(tp_model_actor_class=actor_class)

        class Entry:
            max_tp = limit

        evaluator._entry = lambda *a, **k: Entry()
        return evaluator

    def test_off_by_default(self):
        assert self._evaluator().tp_enabled is False

    def test_on_when_an_actor_is_named(self):
        assert self._evaluator("pkg.TPActor").tp_enabled is True

    def test_a_disabled_cluster_reports_no_degree(self):
        # Not just "doesn't route to the TP actor": no degree at all, so the
        # placement that follows never rounds a GPU count up to a shardable one.
        assert self._evaluator().max_tp("k") is None

    def test_a_disabled_cluster_ignores_a_configured_degree(self):
        # A per-model `max_tp` cannot switch the feature on for a cluster that
        # never opted in.
        assert self._evaluator().max_tp("k", override=8) is None

    def test_a_disabled_cluster_never_routes_to_the_tp_actor(self):
        assert self._evaluator().actor_class("k", 4, "default.Actor") == "default.Actor"

    def test_an_enabled_cluster_still_does(self):
        evaluator = self._evaluator("pkg.TPActor")

        assert evaluator.actor_class("k", 4, "default.Actor") == "pkg.TPActor"

    def test_a_disabled_cluster_does_not_round_the_gpu_count_up(self):
        # The knock-on that matters: with TP on, a model needing three cards and
        # shardable eight ways is given four, because an uneven split will not
        # run. With TP off there is nothing to split, so three cards is three.
        node = make_node()
        size = 226_700_000_000

        assert len(node.evaluate("m", size, max_tp=self._evaluator().max_tp("m")).gpus) == 3

    def test_an_enabled_cluster_still_rounds_it(self):
        node = make_node()
        size = 226_700_000_000
        limit = self._evaluator("pkg.TPActor").max_tp("m")

        assert len(node.evaluate("m", size, max_tp=limit).gpus) == 4


class TestTheEnvVarIsTheSwitch:
    def test_unset_leaves_it_off(self, monkeypatch):
        import importlib

        monkeypatch.delenv("NDIF_TP_MODEL_ACTOR_CLASS", raising=False)
        from ndif.services.ray.deployments.controller import controller as mod

        importlib.reload(mod)
        assert mod.ControllerDeploymentArgs().tp_model_actor_class is None

    def test_empty_is_treated_as_unset(self, monkeypatch):
        # An operator clearing the variable in a compose file leaves "" behind,
        # which must not resolve to an actor class named "".
        import importlib

        monkeypatch.setenv("NDIF_TP_MODEL_ACTOR_CLASS", "")
        from ndif.services.ray.deployments.controller import controller as mod

        importlib.reload(mod)
        assert mod.ControllerDeploymentArgs().tp_model_actor_class is None

    def test_setting_it_turns_the_feature_on(self, monkeypatch):
        import importlib

        from ndif.services.ray.deployments.controller.cluster.evaluator import (
            TP_MODEL_ACTOR_CLASS,
        )

        monkeypatch.setenv("NDIF_TP_MODEL_ACTOR_CLASS", TP_MODEL_ACTOR_CLASS)
        from ndif.services.ray.deployments.controller import controller as mod

        importlib.reload(mod)
        assert mod.ControllerDeploymentArgs().tp_model_actor_class == TP_MODEL_ACTOR_CLASS


class TestDeterminismAcrossProcesses:
    """What has to match when a block runs in more than one process at once.

    Under tensor parallelism the ranks must draw the same numbers and take the
    same branches, or they compute from different things and the model's own
    all-reduces combine them — wrong on every rank, not merely inconsistent. Once
    the block moves to a sandbox runner, "the process that must match" stops
    being the rank and becomes the runner.
    """

    def test_hash_randomization_is_pinned_for_a_rank(self):
        # Python randomizes string hashing per process, so set/dict iteration
        # order differs between processes. A block that iterates a set of module
        # names then takes a different path on each rank -- a hang, not a wrong
        # answer.
        from ndif.services.ray.tp.common import rank_env

        assert rank_env([0, 1], 0, 29500)["PYTHONHASHSEED"] == "0"

    def test_hash_randomization_is_pinned_for_a_runner(self):
        import inspect

        from ndif.services.ray.sandbox import host

        assert 'env["PYTHONHASHSEED"] = "0"' in inspect.getsource(host.spawn)

    def test_there_is_one_definition_of_seeding(self):
        # The tensor-parallel name is kept because it reads well at its call
        # sites, but it must not be a second implementation.
        from ndif.services.ray.deployments.modeling.nns import seed_block
        from ndif.services.ray.tp.common import seed_ranks

        assert seed_ranks is seed_block

    def test_no_seed_leaves_the_rng_alone(self):
        # The ordinary single-process case: two identical requests are *meant* to
        # draw differently, and seeding every one would turn repeated sampling
        # into repeated identical samples.
        import torch

        from ndif.services.ray.deployments.modeling.nns import seed_block

        torch.manual_seed(1234)
        first = torch.randn(3)
        seed_block(None)
        second = torch.randn(3)

        assert not torch.equal(first, second)

    def test_a_seed_makes_two_processes_agree(self):
        import torch

        from ndif.services.ray.deployments.modeling.nns import seed_block

        seed_block(99)
        first = torch.randn(3)
        seed_block(99)
        second = torch.randn(3)

        assert torch.equal(first, second)

    def test_the_executor_seeds_before_the_block_not_before_deserializing(self):
        # What must match across processes is the state the *user's* code draws
        # from, so the seed lands as late as possible.
        import inspect

        from ndif.services.ray.deployments.modeling import nns

        source = inspect.getsource(nns.execute_traced_block)
        assert source.index("seed_block(seed)") < source.index("tracer.execute")

    def test_an_ordinary_deployment_sends_no_seed(self):
        # Two identical requests are meant to draw differently; only a group
        # needs its processes to agree.
        import inspect

        from ndif.services.ray.sandbox.model import SandboxModelDeployment

        source = inspect.getsource(SandboxModelDeployment.execute)
        assert "self.run_in_runner(request, None)" in source

    def test_the_runner_tolerates_a_host_that_sends_none(self):
        # The wire tuple grew a field; a runner must not require it.
        import inspect

        from ndif.services.ray.sandbox import runner

        source = inspect.getsource(runner.Runner.handle)
        assert "*rest" in source and "rest[0] if rest else None" in source


class TestTheSandboxedTensorParallelActor:
    """One actor serving trusted and untrusted requests on a sharded model.

    The fork is per request, exactly as the single-GPU sandbox actor's is. That is
    what keeps the controller choosing on one axis — how the *weights* are placed
    — while each actor decides per request where the *code* runs.

    Hosting a runner is shared with the single-GPU actor now
    (`SandboxHost`), so what these check is the part that is genuinely this
    class's: its dispatch, its two hooks, and an `interrupt` whose order is
    reversed.
    """

    def _source(self, name):
        import inspect

        from ndif.services.ray.tp.model import SandboxedTPModelDeployment

        return inspect.getsource(getattr(SandboxedTPModelDeployment, name))

    def test_it_is_a_tensor_parallel_actor_that_hosts_runners(self):
        from ndif.services.ray.sandbox.model import SandboxHost
        from ndif.services.ray.tp.model import (
            SandboxedTPModelDeployment,
            TPModelDeployment,
        )

        assert issubclass(SandboxedTPModelDeployment, TPModelDeployment)
        assert issubclass(SandboxedTPModelDeployment, SandboxHost)

    def test_a_group_still_cannot_be_cached(self):
        # Inherited, and it must be: the ranks' devices are fixed when their
        # processes start whether or not the block is sandboxed.
        from ndif.services.ray.tp.model import SandboxedTPModelDeployment

        assert SandboxedTPModelDeployment.CACHEABLE is False

    def test_a_trusted_request_takes_the_in_process_path(self):
        # No runner, no barrier: every rank builds and runs the block, which is
        # what makes the gather decisions agree with nothing sent.
        source = self._source("_dispatch")

        assert "if request.trusted:" in source
        assert "super()._dispatch(request, seed)" in source

    def test_an_untrusted_request_goes_to_the_shared_runner(self):
        assert "self.run_in_runner(request, seed)" in self._source("_dispatch")

    def test_its_pool_is_sized_in_groups(self):
        # One runner serves the whole group, so a pool entry holds `tp_size`
        # connections rather than being one process per rank.
        assert "peers=self.tp_size" in self._source("__init__")

    def test_the_shards_are_told_where_the_runner_is(self):
        assert "self.group.sandbox(sandbox.path" in self._source("before_payload")

    def test_the_group_is_released_only_after_the_block_is_sent(self):
        assert "self.commit()" in self._source("after_payload")

    def test_interrupt_asks_the_group_first_then_stops_the_runner(self):
        # The flag lets ranks already in a forward unwind together at the shared
        # checkpoint; stopping the runner covers a block stuck in plain Python,
        # where no checkpoint will fire. The single-GPU host does the opposite,
        # having no collective to strand.
        source = self._source("interrupt")

        assert source.index("TPModelDeployment.interrupt(self)") < source.index("stop()")


class TestTheRunnerIsDrivenTheSameWayByBoth:
    """The order both hosts depend on, now stated once.

    These used to be asserted against two copies of the same body. The copies
    drifted exactly once, and it cost a correctness bug.
    """

    def _body(self):
        import inspect

        from ndif.services.ray.sandbox.model import SandboxHost

        return inspect.getsource(SandboxHost.run_in_runner)

    def test_the_host_connects_before_its_hook_runs(self):
        # Load-bearing for the group: the runner takes its *first* connection as
        # the one that sends the payload, so rank 0 must be connected before any
        # shard is told where to find it.
        body = self._body()

        assert body.index("sandbox.connection()") < body.index("self.before_payload(")

    def test_the_block_is_sent_between_the_two_hooks(self):
        body = self._body()

        assert body.index("self.before_payload(") < body.index("connection.send(")
        assert body.index("connection.send(") < body.index("self.after_payload()")

    def test_nothing_runs_until_after_the_second_hook(self):
        # `after_payload` is where a group releases its shards; the model must not
        # have started before it.
        body = self._body()

        assert body.index("self.after_payload()") < body.index("driver.pump")

    def test_a_request_in_flight_is_cancellable(self):
        # `kill` is gated on execution_ident; without it a cancel or preempt is
        # silently dropped and the block runs to completion.
        assert "self.execution_ident = threading.current_thread().ident" in self._body()

    def test_the_connection_is_closed_however_it_ends(self):
        body = self._body()

        assert "finally:" in body and "connection.close()" in body


class TestEveryRankIsSeeded:
    """Every process that runs the model must draw the same numbers.

    Not hygiene: ranks that sample differently generate different tokens, and the
    model's own all-reduces then sum activations computed from different
    sequences — wrong on every rank rather than merely inconsistent. Many
    checkpoints ship ``do_sample: true``, so it reaches requests that never asked
    to sample.

    The untrusted path shipped without rank 0 seeding itself. It was invisible
    because the *shards* seed on both paths and rank 0 seeds on the trusted one,
    so every individual file looked right; only the pairing was wrong.
    """

    def test_the_trusted_path_seeds_this_rank(self):
        import inspect

        from ndif.services.ray.tp.model import TPModelDeployment

        assert "seed_ranks(seed)" in inspect.getsource(TPModelDeployment._dispatch)

    def test_the_untrusted_path_seeds_this_rank_too(self):
        # The block runs in the runner, but the model still runs here.
        import inspect

        from ndif.services.ray.tp.model import SandboxedTPModelDeployment

        assert "seed_ranks(seed)" in inspect.getsource(
            SandboxedTPModelDeployment.before_payload
        )

    def test_it_is_seeded_before_anyone_is_released(self):
        # `before_payload` runs before the block is sent and before
        # `after_payload` commits the group — so seeding lands ahead of any rank
        # entering a forward, by the shared body's construction.
        import inspect

        from ndif.services.ray.sandbox.model import SandboxHost

        body = inspect.getsource(SandboxHost.run_in_runner)
        assert body.index("self.before_payload(") < body.index("self.after_payload()")

    def test_the_runner_gets_the_same_number(self):
        # Three consumers of one seed: this rank and the shards (in
        # `before_payload`), and the block itself, shipped in the payload tuple.
        import inspect

        from ndif.services.ray.sandbox.model import SandboxHost
        from ndif.services.ray.tp.model import SandboxedTPModelDeployment

        hook = inspect.getsource(SandboxedTPModelDeployment.before_payload)
        assert "self.group.sandbox(sandbox.path, request.env, seed)" in hook
        assert "seed_ranks(seed)" in hook
        assert "str(self.dtype), seed" in inspect.getsource(SandboxHost.run_in_runner)

    def test_both_shard_paths_seed(self):
        # The other half of the pairing, so a future edit to shard.py that drops
        # one is caught here rather than by a hung group.
        import inspect

        from ndif.services.ray.tp import shard

        source = inspect.getsource(shard)
        assert source.count("seed_ranks(seed)") >= 2, (
            "a shard must seed on the trusted and the sandboxed path alike"
        )


class TestValuesFromTheRunnerLandOnThisHost:
    """A tensor's device is absolute, and one runner serves a whole group.

    `Fanout` sends identical bytes to every rank, so a value that round-trips
    through the runner reaches rank 1 still labelled with rank 0's card. The shard
    mixes it into its own forward and torch raises "expected all tensors to be on
    the same device, but found cuda:1 and cuda:0" -- which is exactly what the
    untrusted tensor-parallel path did. Invisible on one GPU, where the only host
    *is* cuda:0.

    The policy lives in the deserializer (`torch.load`'s ``map_location``) rather
    than in a walk over each message afterwards: relocating after the fact rebuilds
    the tensor on the sender's card first, which under tensor parallelism is
    another rank's memory budget.
    """

    def test_the_host_reads_the_runner_in_exactly_one_place(self):
        # The premise the seam rests on: setting `map_location` on the connection
        # covers everything only while that connection is the sole reader.
        import inspect

        from ndif.services.ray.sandbox import driver

        source = inspect.getsource(driver)
        assert source.count("connection.recv()") == 1, (
            "a second read from the runner would bypass the device policy"
        )

    def test_the_host_declares_where_tensors_land(self):
        import inspect

        from ndif.services.ray.sandbox.driver import SandboxDriver

        source = inspect.getsource(SandboxDriver.pump)
        assert "connection.map_location = self.model.device" in source

    def test_it_is_set_before_anything_is_read(self):
        import inspect

        from ndif.services.ray.sandbox.driver import SandboxDriver

        source = inspect.getsource(SandboxDriver.pump)
        assert source.index("map_location") < source.index("next_event")

    def test_the_deserializer_honours_it(self):
        import inspect

        from ndif.services.ray.sandbox.protocol import decode

        source = inspect.getsource(decode)
        assert "map_location=map_location" in source

    def test_a_message_lands_where_the_receiver_asked(self):
        # The behaviour itself, not its wiring. CPU-only, so it runs anywhere:
        # "somewhere else" is expressed as an explicit map_location.
        import torch

        from ndif.services.ray.sandbox.protocol import decode, encode

        blob = encode(("RESUME", 0, (torch.ones(4),), None))

        assert decode(blob)[2][0].device.type == "cpu"
        assert decode(blob, map_location="cpu")[2][0].device.type == "cpu"

    def test_the_runner_leaves_tensors_where_they_are(self):
        # None is the runner's setting: the block is meant to compute on GPU, and
        # pulling activations to the CPU would move the user's arithmetic there and
        # stop matching the in-process path.
        from ndif.services.ray.sandbox.protocol import Channel

        assert Channel(None).map_location is None

    def test_the_assembly_relocation_is_kept(self):
        # Not redundant with map_location: the batcher and tokenizer run on the
        # host, so those tensors are made here and never crossed the wire.
        import inspect

        from ndif.services.ray.sandbox.driver import SandboxDriver

        assert "self._to_device(" in inspect.getsource(SandboxDriver._assemble)


class TestAShardFailureIsNotReportedAsABrokenPipe:
    """Whose error the operator is shown when a shard dies mid-request.

    A shard that raises closes the shared runner's connection, so rank 0's own
    failure is a broken pipe -- true, and useless. The cause is in the shards'
    reports, which are logged at debug because normally the user already has the
    same error from rank 0. When rank 0 only saw the socket, that reasoning
    inverts.
    """

    def test_rank_zeros_exception_is_kept_not_just_a_flag(self):
        import inspect

        from ndif.services.ray.tp.model import TPModelDeployment

        source = inspect.getsource(TPModelDeployment.execute)
        assert "self.rank_zero_error = exception" in source

    def test_a_transport_failure_promotes_the_shard_error(self):
        import inspect

        from ndif.services.ray.tp.model import TPModelDeployment

        source = inspect.getsource(TPModelDeployment._await_shards)
        assert "BrokenPipeError, ConnectionError" in source
        assert source.index("logger.error") < source.index("logger.debug")

    def test_an_ordinary_failure_stays_quiet(self):
        # The common case is the user's own error, raised identically on every
        # rank; logging every shard's copy at error would bury the real ones.
        import inspect

        from ndif.services.ray.tp.model import TPModelDeployment

        assert "logger.debug" in inspect.getsource(TPModelDeployment._await_shards)


class TestRunnerDiagnosticsSurvive:
    def test_stderr_is_never_discarded(self):
        # The block's output goes back as PRINT events, so anything on the
        # runner's real stderr is the process failing on its own account --
        # including `serve`'s handler, the only report of a request it could not
        # finish. DEVNULL made a dead runner look like a hung one.
        import inspect

        from ndif.services.ray.sandbox import host

        source = inspect.getsource(host.spawn)
        assert "stderr=None" in source
        assert "stderr=output" not in source


class TestWhoseErrorTheOperatorSees:
    """A shard that dies takes the shared runner's socket with it.

    Rank 0 then reports a broken pipe — true, and a description of the symptom.
    The cause is in the shards' reports, which are normally debug-level because
    the user already has the same error from rank 0. When rank 0 only saw the
    socket, that reasoning inverts and the shard's error is all there is.
    """

    def _verdict(self, rank_zero_error, caplog):
        import logging

        deployment = TestShardSettleVerdict._deployment(
            raised=["rank 1: RuntimeError: expected all tensors on the same device"],
            lost=[],
            rank_zero_raised=True,
            rank_zero_error=rank_zero_error,
        )
        with caplog.at_level(logging.DEBUG, logger="ndif.modeling"):
            deployment._await_shards()
        return caplog.records

    def test_a_broken_pipe_promotes_the_shard_error(self, caplog):
        records = self._verdict(BrokenPipeError(32, "Broken pipe"), caplog)
        errors = [r for r in records if r.levelname == "ERROR"]

        assert errors, "the only account of what happened was left at debug"
        assert "same device" in errors[0].getMessage()

    def test_an_ordinary_failure_stays_at_debug(self, caplog):
        records = self._verdict(ValueError("the user's own bug"), caplog)

        assert not [r for r in records if r.levelname == "ERROR"]
        assert [r for r in records if r.levelname == "DEBUG"]

    def test_it_still_returns_the_group_healthy_either_way(self, caplog):
        # Promotion is a logging decision, not a verdict: a block that raised on
        # every rank must not cost the replica.
        for error in (BrokenPipeError(32, "Broken pipe"), ValueError("bug")):
            deployment = TestShardSettleVerdict._deployment(
                raised=["rank 1: boom"], lost=[], rank_zero_raised=True,
                rank_zero_error=error,
            )
            assert deployment._await_shards() is True


class TestARunnerInheritsNothingSensitive:
    """The runner executes other people's Python, and `os.environ` is the first
    place to look.

    The actor's environment is the operator's. Measured on a live deployment it
    carried `HF_TOKEN` and `NDIF_INFLUX_TOKEN`; a production one would add
    object-store credentials and `NDIF_POSTGRES_URL`. It used to be copied
    wholesale into every runner.
    """

    def _env(self, monkeypatch, **values):
        from ndif.services.ray.sandbox.host import runner_env

        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return runner_env()

    @pytest.mark.parametrize(
        "name",
        [
            "HF_TOKEN",
            "NDIF_INFLUX_TOKEN",
            "NDIF_POSTGRES_URL",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "NDIF_OBJECT_STORE_SECRET_KEY",
        ],
    )
    def test_a_credential_does_not_reach_the_runner(self, monkeypatch, name):
        assert name not in self._env(monkeypatch, **{name: "secret"})

    def test_it_is_an_allowlist_not_a_denylist(self):
        # A denylist is wrong by construction here: the next credential someone
        # adds to the actor's environment would be inherited by default.
        import inspect

        from ndif.services.ray.sandbox import host

        source = inspect.getsource(host.runner_env)
        assert "RUNNER_ENV" in source
        assert "os.environ[name]" in source

    def test_an_unknown_variable_is_dropped(self, monkeypatch):
        assert "SOMETHING_NEW" not in self._env(monkeypatch, SOMETHING_NEW="x")

    @pytest.mark.parametrize(
        "name,value",
        [
            ("CUDA_VISIBLE_DEVICES", "1,3"),
            ("HF_HOME", "/disk/hf"),
            ("PATH", "/usr/bin"),
        ],
    )
    def test_what_a_runner_needs_still_arrives(self, monkeypatch, name, value):
        # Device numbering especially: under tensor parallelism a device index is
        # a position in this list, not a physical card, so a runner that lost it
        # would disagree with the host it is driving.
        assert self._env(monkeypatch, **{name: value})[name] == value

    @pytest.mark.parametrize(
        "name", ["RANK", "LOCAL_RANK", "WORLD_SIZE", "MASTER_ADDR", "MASTER_PORT"]
    )
    def test_the_runner_does_not_claim_to_be_a_rank(self, monkeypatch, name):
        # Rank 0 sets these on itself to join the process group. A runner spawned
        # from it inherited them and started life claiming to be rank 0 of a group
        # it is not in -- which accelerate and transformers read to decide they
        # are in a distributed launch.
        assert name not in self._env(monkeypatch, **{name: "0"})

    def test_spawn_uses_it(self):
        import inspect

        from ndif.services.ray.sandbox import host

        source = inspect.getsource(host.spawn)
        assert "env = runner_env()" in source
        assert "dict(os.environ)" not in source
