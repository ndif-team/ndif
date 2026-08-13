"""The tensor-parallel model actor.

Where the base actor spreads a model over its GPUs with accelerate — whole layers
on each card, run one after another — this one shards *within* each layer with
transformers tensor parallelism, so the GPUs work on the same layer at once. That
needs one process per GPU, and this actor **is rank 0**: it holds a shard of the
weights, spawns the other ranks (see [`ShardGroup`][ndif.services.ray.tp.host.ShardGroup]),
and runs the user's block itself.

Being rank 0 rather than a coordinator over N children is what keeps this small:
``run()`` — the statuses, the timeout race, the metrics, the upload — is inherited
untouched, and the model, the block, and the values it saves are all right here.
Only three things are genuinely different:

* **Every rank runs the block.** Whether a sharded activation is gathered depends
  on where the user's interventions are parked, so all ranks must make that
  decision identically or NCCL deadlocks. The shards therefore execute the same
  Python; rank 0 alone answers the client and uploads.
* **Cancelling is cooperative.** ``kill_thread`` on rank 0 mid-collective would
  strand the others, so the ranks stop at a shared checkpoint instead (see
  [`AbortController`][ndif.services.ray.tp.common.AbortController]), with a
  restart as the fallback for a rank too wedged to reach one.
* **The GPUs are renamed.** transformers picks a rank's device by ``LOCAL_RANK``
  used as a CUDA index, so the whole group runs under a ``CUDA_VISIBLE_DEVICES``
  listing only the assigned cards. Inside these processes a device index is a
  position in that list, not a physical GPU id.

HOT<->WARM caching is not supported yet — see ``to_cache``.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import random
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

import ray
import torch

from nnsight.modeling.huggingface import HuggingFaceModel
from nnsight.schema.response import Status

from ....common.metrics import ModelLoadTimeMetric
from ....common.telemetry import elapsed_ms
from ....common.types import MODEL_KEY

from ..deployments.modeling.base import BaseModelDeployment
from ..sandbox.model import SandboxHost
from ..deployments.modeling.util import set_process_limits, verify_device_placement
from .common import (
    AbortController,
    AbortedError,
    load_sharded_model,
    rank_env,
    seed_ranks,
    visible_devices,
)
from .host import ShardError, ShardGroup

if TYPE_CHECKING:
    from ....common.schema.request import BackendRequestModel

logger = logging.getLogger("ndif.modeling")

# How long the shards are given to report a request finished before the group is
# assumed wedged and the replica restarted (expensive — a full multi-GPU reload —
# but always terminal).
#
# Short on purpose. Two cases reach this: a request that ended normally, where the
# ranks leave the last collective together and the answer is already in flight;
# and an abort, where each rank has one forward pass to run before its next
# checkpoint. Neither takes long, and the wait happens on the actor's event loop,
# so a generous timeout would stall every other call into this actor — including
# the cancel that might be trying to free it.
SETTLE_TIMEOUT_SECONDS = 30.0


class TPModelDeployment(BaseModelDeployment):
    """A model actor that is rank 0 of a tensor-parallel group."""

    # A group cannot be parked. Restoring may land on a different set of GPUs,
    # and every rank's device is fixed when its process starts -- there is no
    # way to move a live group to other cards. Freeing one means tearing it
    # down, so the controller evicts these outright rather than caching them.
    CACHEABLE = False

    def __init__(
        self,
        model_key: MODEL_KEY,
        execution_timeout: Optional[float] = None,
        gpu_mem_bytes_by_id: Optional[Dict[int, int]] = None,
        dtype: str = "bfloat16",
        **kwargs: Any,
    ) -> None:
        gpu_mem_bytes_by_id = gpu_mem_bytes_by_id or {}
        self.tp_gpu_ids = sorted(gpu_mem_bytes_by_id)
        # The allocation is the degree. The controller only routes a replica here
        # when the model shards evenly into exactly the cards it assigned, so
        # there is nothing to choose and nothing to validate against.
        self.tp_size = len(self.tp_gpu_ids)

        if self.tp_size < 2:
            raise ValueError(
                "a tensor-parallel replica needs at least 2 GPUs, got "
                f"{self.tp_gpu_ids}. One card wants the ordinary actor."
            )

        # Before any CUDA call in this process. Safe here because the controller
        # sets RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES, so Ray leaves this
        # alone, and nothing touches CUDA before load_from_disk.
        os.environ["CUDA_VISIBLE_DEVICES"] = visible_devices(self.tp_gpu_ids)

        # Rank 0 owns the first assigned card, which is now visible index 0. The
        # base's per-GPU bookkeeping (allocation caps, peak-memory attribution)
        # is rewritten to that one device: the others belong to other processes
        # and are neither visible to nor capped by this one.
        self.tp_budgets = dict(gpu_mem_bytes_by_id)
        rank_zero_budget = gpu_mem_bytes_by_id[self.tp_gpu_ids[0]]

        self.group: Optional[ShardGroup] = None
        self.abort: Optional[AbortController] = None
        # Per-request, set in `execute`; here so `cleanup` is safe on a path that
        # never reached one (a load failure, or a prepare that threw first).
        self.committed = False
        self.rank_zero_raised = False
        self.rank_zero_error: Optional[BaseException] = None
        self.dispatched = False
        # Whether the shards confirmed they went back to idle after a request
        # that never ran. False means one didn't answer, which is the only
        # version of "didn't run" that costs the replica.
        self.stood_down = True

        super().__init__(
            model_key,
            execution_timeout=execution_timeout,
            gpu_mem_bytes_by_id={0: rank_zero_budget},
            dtype=dtype,
            **kwargs,
        )

    # -- loading -----------------------------------------------------------

    def load_from_disk(self) -> HuggingFaceModel:
        """Bring up the whole group, then load rank 0's own shard.

        The shards are started first and block in the process-group rendezvous;
        this rank joins it from inside ``load_sharded_model``, which is where
        transformers initializes distributed and shards the weights.
        """
        started_at = time.time()

        torch.cuda.set_device(0)
        set_process_limits(self.gpu_mem_bytes_by_id)

        self.group = ShardGroup(
            model_key=self.model_key,
            gpu_ids=self.tp_gpu_ids,
            dtype=self.dtype_name,
            tp_size=self.tp_size,
            load_kwargs=self.kwargs,
            gpu_mem_bytes_by_id=self.tp_budgets,
        )
        # The same environment the shards were launched with, for rank 0 —
        # transformers reads it to join the group, so one definition serves all
        # ranks rather than this one keeping its own copy in step.
        os.environ.update(rank_env(self.tp_gpu_ids, 0, self.group.master_port))
        # Only waits for the shards to come up, not to load: they block in the
        # process-group rendezvous until this rank joins it below.
        self.group.start()

        model = load_sharded_model(
            self.model_key, self.dtype_name, tp_size=self.tp_size, **self.kwargs
        )
        self.group.wait_ready()

        torch.cuda.synchronize()
        verify_device_placement(model, self.gpu_mem_bytes_by_id.keys())

        # Installed after the model exists and before any request, on every rank.
        self.abort = AbortController(model._module, source=True)

        logger.info(
            f"Loaded {self.model_key} tensor-parallel over GPUs {self.tp_gpu_ids} "
            f"(tp_size={self.tp_size})"
        )
        ModelLoadTimeMetric.update(
            model_key=self.model_key,
            duration_ms=elapsed_ms(started_at),
            load_type="initial",
            num_gpus=self.tp_size,
        )
        return model

    async def to_cache(self) -> None:
        raise NotImplementedError(
            "a tensor-parallel replica cannot be parked (HOT->WARM) yet: restoring "
            "may reassign GPUs, and the group's device mapping is fixed when its "
            "processes start. Pin the deployment so it is never evicted."
        )

    def from_cache(self, gpu_mem_bytes_by_id: Dict[int, int]) -> None:
        raise NotImplementedError(
            "a tensor-parallel replica is never cached; see to_cache."
        )

    # -- one request -------------------------------------------------------

    def execute(self, request: "BackendRequestModel") -> "tuple[bytes, float]":
        """Run the block on every rank, and return rank 0's saved values.

        Every rank — this one included — proves it can *build* the block before
        any of them starts a forward pass; past that point the ranks are joined at
        the hip by NCCL and a rank that drops out leaves the others blocked until
        the execution timeout fires. The base's ``execute`` already has that seam
        in it (:meth:`commit`), so all this adds is the group either side of it.
        """
        if self.group is None or not self.group.healthy:
            raise RuntimeError("the tensor-parallel shard group is not running")

        # One seed for the whole group, so a sampled generation makes the same
        # choices everywhere. Ranks that disagree here do not merely return
        # different text — they go on to all-reduce activations computed from
        # different tokens. See common.seed_ranks.
        seed = random.getrandbits(31)

        self.committed = False
        self.rank_zero_raised = False
        self.rank_zero_error = None
        self.dispatched = False
        self.stood_down = True
        try:
            # Set *before* the call, not after it succeeds: `prepare` sends to
            # every shard and only then collects, so a payload one shard rejects
            # leaves the others already parked on a GO. "Did any shard hear about
            # this request" is the question the stand-down below has to answer,
            # and it is true from here on.
            self.dispatched = True
            return self._dispatch(request, seed)
        except BaseException as exception:
            # Recorded for :meth:`_await_shards`, which runs after this returns
            # and has to tell "the user's block raised, on every rank" from "a
            # shard failed while this rank was fine". The exception itself is kept
            # too: a transport failure here usually means a *shard* died and took
            # the socket with it, so which one it is decides whose error is worth
            # reporting.
            self.rank_zero_raised = True
            self.rank_zero_error = exception
            raise
        finally:
            self.abort.disarm()
            if self.dispatched and not self.committed:
                # The shards built the block and this rank did not go ahead —
                # it failed to build the same thing, or the request died between
                # the two. They are waiting on a GO that is never coming, so send
                # them back to idle rather than leaving them parked until their
                # hour is up, and wait for them to say they got there.
                #
                # `prepare` is inside the try for this: a shard that fails to
                # deserialize raises ShardError out of it, and the shards that
                # *did* prepare are then holding exactly as they are here. With
                # the call above the try they were left parked for an hour, which
                # is the failure the two-phase protocol exists to prevent.
                lost = self.group.release()
                if lost:
                    logger.error(
                        f"TP shards did not stand down on {self.model_key}: {lost}"
                    )
                    self.stood_down = False

    def _dispatch(self, request: "BackendRequestModel", seed: int) -> "tuple[bytes, float]":
        """Hand this request to the group and run it.

        The seam a sandboxed subclass replaces. Everything around it in
        :meth:`execute` — the seed, the stand-down bookkeeping, telling a shard
        failure from a block that raised on every rank — is the same whoever runs
        the block, so it stays there.

        Here: every rank builds and runs the block itself, which is what makes the
        gather decisions agree without anything being sent.
        """
        self.group.prepare(request.payload, request.compress, request.env, seed)
        seed_ranks(seed)
        return super().execute(request)

    def commit(self) -> None:
        """Release the other ranks into the forward, and arm the abort checkpoint.

        The base calls this once the block is built and immediately before it
        runs, which is exactly the moment the group can safely commit.
        """
        self.group.go()
        self.abort.arm()
        self.committed = True

    def interrupt(self) -> None:
        """Ask every rank to stop at its next checkpoint.

        Deliberately *not* the base's ``kill_thread``: rank 0's execution thread
        is usually inside a collective, and killing it there leaves every other
        rank blocked in NCCL with no way back. Setting the flag instead lets the
        ranks unwind together (:meth:`cleanup` waits for that, and restarts the
        replica if they don't).
        """
        if self.abort is not None:
            self.abort.request()

    def cleanup(self) -> None:
        """Wait for the group to come back to rest, then reclaim as usual.

        A request only ends when *every* rank has left it. Starting the next one
        while a shard is still unwinding would put the ranks at different points
        in the collective stream, which is unrecoverable — so a group that has
        not settled within the grace period is restarted rather than reused.
        """
        settled = self._await_shards()
        if self.abort is not None:
            self.abort.disarm()

        super().cleanup()

        if not settled:
            logger.error(
                f"TP group for {self.model_key} did not settle within "
                f"{SETTLE_TIMEOUT_SECONDS}s; restarting the replica"
            )
            self.restart()

    def _await_shards(self) -> bool:
        """Whether the group is back at rest and can serve another request.

        Not the same question as whether the request succeeded. Every rank runs
        the user's block, so a block that raises raises *everywhere* — each shard
        reports it and returns to its idle loop, which is a settled group and a
        perfectly ordinary failed request. Treating that as wedged tore down a
        healthy multi-GPU replica every time someone had a bug in their code, and
        the *next* request was the one that failed.

        What is not survivable is the ranks disagreeing: a shard that raised while
        this rank did not has left the collective stream somewhere this one has
        not, and there is no way back. Nor is a shard that never answered.
        """
        if self.group is None:
            return True

        if not self.committed:
            # Nothing ran, so there is nothing to collect — the shards either
            # never prepared or were stood down by `execute`, and either way they
            # are not going to send a DONE. Asking anyway meant every failed
            # deserialization timed out here and restarted the replica, which is
            # the opposite of what standing them down was for.
            return self.stood_down

        try:
            raised, lost = self.group.collect(timeout=SETTLE_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Failed to collect from the TP shards")
            return False

        if lost:
            logger.error(f"TP shards did not report on {self.model_key}: {lost}")
            return False

        if raised and not self.rank_zero_raised:
            logger.error(
                f"TP shard failures on {self.model_key} that rank 0 did not hit — "
                f"the ranks have diverged: {raised}"
            )
            return False

        if raised:
            # Normally the user already has this error from rank 0, so it is an
            # operator detail and belongs at debug -- the expected shape of a
            # failed request, not a fault in the deployment.
            #
            # Unless rank 0's own failure was the *socket*. A shard that dies
            # takes the shared runner's connection with it, and what rank 0 then
            # reports is a broken pipe: a true statement about a symptom, with the
            # cause sitting in `raised` where nobody looks. Say it loudly in that
            # case, because it is the only description of what actually happened.
            if isinstance(self.rank_zero_error, (BrokenPipeError, ConnectionError)):
                logger.error(
                    f"TP shard failures on {self.model_key} are the likely cause of "
                    f"rank 0's {type(self.rank_zero_error).__name__}: {raised}"
                )
            else:
                logger.debug(
                    f"TP shards raised with rank 0 on {self.model_key}: {raised}"
                )

        return self.group.healthy

    def format_error(self, exception: BaseException) -> "tuple[str, bool]":
        if isinstance(exception, AbortedError):
            # The user is told separately why the run was stopped (timeout or
            # cancel); this is just how the thread found out. Not fatal — the
            # ranks unwound together and the group is still usable.
            return "Your job was stopped before it finished.", False
        if isinstance(exception, ShardError):
            return (
                "Your job could not be prepared on every GPU of this model:\n"
                f"{exception}",
                False,
            )
        return super().format_error(exception)

    def restart(self) -> None:
        """Tear the group down before letting Ray restart this actor."""
        if self.group is not None:
            self.group.stop()
            self.group = None
        self._destroy_process_group()
        super().restart()

    def _destroy_process_group(self) -> None:
        try:
            import torch.distributed as dist

            if dist.is_initialized():
                dist.destroy_process_group()
        except Exception:
            logger.debug("Could not destroy the process group", exc_info=True)
        gc.collect()
        torch.cuda.empty_cache()

    def __del__(self) -> None:
        if getattr(self, "group", None) is not None:
            self.group.stop()


class SandboxedTPModelDeployment(SandboxHost, TPModelDeployment):
    """A tensor-parallel actor that serves trusted *and* untrusted requests.

    The fork is per request, exactly as the single-GPU sandbox actor's is: a
    trusted block runs on every rank in-process, an untrusted one runs once in a
    runner that every rank is a host to. That is why "sharded" and "sandboxed"
    stay one axis for the controller rather than a four-way matrix — it picks an
    actor by how the *weights* are placed, and each actor decides per request
    where the *code* runs.

    The untrusted path is the cheaper one to reason about, which is worth saying
    because it looks like the exotic one. Every rank keeps its own mirror of where
    the workers are parked, so each answers "is anyone waiting here?" locally and
    the ranks agree about gathering without sending anything. What the runner adds
    is a barrier: it serves each worker once for the whole group rather than once
    per rank (see [`Fanout`][ndif.services.ray.sandbox.protocol.Fanout]).

    Everything about *hosting* a runner comes from
    [`SandboxHost`][ndif.services.ray.sandbox.model.SandboxHost]; what is left
    here is the two moments a group needs that one host does not, and an
    `interrupt` whose order is reversed.
    """

    def __init__(self, *args: Any, pool_size: Optional[int] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # One runner serves the whole group, so a pool entry holds `tp_size`
        # connections: sized in *groups*, not processes.
        self.open_pool(pool_size, peers=self.tp_size)

    def _dispatch(self, request: "BackendRequestModel", seed: int) -> "tuple[bytes, float]":
        if request.trusted:
            return super()._dispatch(request, seed)
        return self.run_in_runner(request, seed)

    def before_payload(
        self, request: "BackendRequestModel", sandbox: Any, seed: int
    ) -> None:
        """Point the shards at the runner, and seed this rank.

        Order is load-bearing on both sides. This rank is already connected —
        `run_in_runner` connects before calling this — because the runner takes
        its *first* connection as the one that sends the payload. And the shards
        answer READY before anything is in a collective, so a shard that cannot
        reach the runner is a failure this rank can report without leaving anyone
        blocked.
        """
        self.group.sandbox(sandbox.path, request.env, seed)
        # This rank's own RNG, from the same number the shards and the runner get.
        # The block runs in the runner, but the *model* still runs here, and its
        # sampling draws from this process — so rank 0 has to be seeded for the
        # same reason every shard is (see shard.py's SANDBOX branch). Missing it
        # left rank 0 sampling a different token from the ranks it was about to
        # all-reduce with.
        seed_ranks(seed)

    def after_payload(self) -> None:
        # Releases the shards into the forward and arms the abort checkpoint. The
        # base does this for a trusted request from inside its own `execute`;
        # there is no base `execute` here, so the moment has to be chosen — and
        # this is it, the last point before any rank runs anything.
        self.commit()

    def interrupt(self) -> None:
        """Stop the group, then the runner holding its block.

        The flag first, so ranks already inside a forward unwind together at the
        shared checkpoint — the opposite order to the single-GPU host, which has
        no collective to strand. Then the runner, which covers what the flag
        cannot: a block stuck in plain Python, where no rank is in a forward and
        no checkpoint will fire.

        Stopping the runner is safe here in a way it would not be with one runner
        per rank — there is a single one, so every rank's socket closes at the same
        moment and they unwind symmetrically rather than one diverging.
        """
        TPModelDeployment.interrupt(self)
        if self.execution_sandbox is not None:
            self.execution_sandbox.stop()


@ray.remote(num_cpus=1, max_restarts=-1)
class SandboxedTPModelActor(SandboxedTPModelDeployment):
    """The deployable tensor-parallel actor that also sandboxes untrusted code."""

    pass


@ray.remote(num_cpus=1, max_restarts=-1)
class TPModelActor(TPModelDeployment):
    """The deployable tensor-parallel actor."""

    pass
