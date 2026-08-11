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

import gc
import logging
import os
import random
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

import ray
import torch

from nnsight.modeling.huggingface import HuggingFaceModel

from ....common.metrics import ModelLoadTimeMetric
from ....common.telemetry import elapsed_ms
from ....common.types import MODEL_KEY
from ..deployments.modeling.base import BaseModelDeployment
from ..deployments.modeling.util import set_process_limits, verify_device_placement
from .common import (
    TP_SIZE,
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
        self.tp_size = TP_SIZE

        if len(self.tp_gpu_ids) != self.tp_size:
            raise ValueError(
                f"a tensor-parallel replica needs exactly {self.tp_size} GPUs, "
                f"got {len(self.tp_gpu_ids)} ({self.tp_gpu_ids}). The controller "
                "sizes a model by how many cards it takes to hold it, which is "
                "not yet the same question as what degree it shards into."
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
            dtype=str(self.dtype).removeprefix("torch."),
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
            self.model_key, self.dtype, tp_size=self.tp_size, **self.kwargs
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

        self.group.prepare(request.payload, request.compress, request.env, seed)
        seed_ranks(seed)
        self.committed = False
        try:
            return super().execute(request)
        finally:
            self.abort.disarm()
            if not self.committed:
                # This rank failed to build what the shards already built — they
                # are waiting on a GO that is never coming, so send them back to
                # idle rather than leaving them parked until their hour is up.
                self.group.release()

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
        """Whether every shard reported the request finished."""
        if self.group is None:
            return True
        try:
            failures = self.group.collect(timeout=SETTLE_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Failed to collect from the TP shards")
            return False
        if failures:
            # The user already has rank 0's answer; this is for the operator.
            logger.error(f"TP shard failures on {self.model_key}: {failures}")
            return False
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


@ray.remote(num_cpus=1, max_restarts=-1)
class TPModelActor(TPModelDeployment):
    """The deployable tensor-parallel actor."""

    pass
