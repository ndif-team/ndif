"""What rank 0 and every shard must do identically.

Anything in here is executed by all ranks in lockstep. Two rules follow from
that and are the reason this module exists rather than the code living on each
side:

* **Load the same way.** Every rank builds the model with the same call, so the
  shards agree on the module tree and the collectives line up.
* **Run the block the same way.** Building and running it come from
  ``deployments.modeling.nns``, shared with the non-parallel actor — the autocast
  dtype in particular has to match, or the ranks reduce different dtypes.

Nothing here may branch on rank except where a collective genuinely has a root
(the abort flag's source).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..sandbox.protocol import Channel

logger = logging.getLogger("ndif.modeling")

# The degree is the number of GPUs the controller assigned: it only places a
# replica tensor-parallel when the model shards evenly into exactly that many
# (see the evaluator's `tp_degree`), so the allocation *is* the degree. This is
# the fallback for a caller that has no allocation to go on.
DEFAULT_TP_SIZE = 2


class AbortedError(Exception):
    """The run was aborted at a checkpoint, on every rank at once."""


def visible_devices(gpu_ids: List[int]) -> str:
    """The ``CUDA_VISIBLE_DEVICES`` value every rank in the group shares.

    transformers picks each rank's device with ``os.environ["LOCAL_RANK"]`` used
    directly as a CUDA index (see
    ``transformers.integrations.tensor_parallel.initialize_tensor_parallelism``),
    so rank *i* must find its GPU at visible index *i*. Restricting the whole
    group to the assigned cards, in a fixed order, is what makes that true — and
    it means an index inside any of these processes is a position in this list,
    not a physical GPU id.
    """
    return ",".join(str(gpu_id) for gpu_id in sorted(gpu_ids))


def rank_env(gpu_ids: List[int], rank: int, master_port: int) -> Dict[str, str]:
    """The distributed environment for one rank of the group."""
    return {
        "CUDA_VISIBLE_DEVICES": visible_devices(gpu_ids),
        "RANK": str(rank),
        "LOCAL_RANK": str(rank),
        "WORLD_SIZE": str(len(gpu_ids)),
        "MASTER_ADDR": "127.0.0.1",
        "MASTER_PORT": str(master_port),
        # Let NCCL abort a collective that hangs rather than blocking forever, so
        # a rank lost mid-run surfaces as an error the actor can restart on.
        "TORCH_NCCL_ASYNC_ERROR_HANDLING": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
    }


def load_sharded_model(
    model_key: str, dtype: Any, tp_size: int = DEFAULT_TP_SIZE, **kwargs: Any
) -> Any:
    """Build this rank's shard of ``model_key``.

    Every rank runs this identically; transformers initializes the process group
    on the first call (reading ``RANK`` / ``LOCAL_RANK`` / ``WORLD_SIZE``) and
    shards the weights onto this rank's device. nnsight notices the sharding and
    gathers activations for intervention code by itself — see
    ``nnsight.modeling.tp``.
    """
    from transformers.distributed import DistributedConfig

    from nnsight.modeling.huggingface import HuggingFaceModel

    model = HuggingFaceModel.from_model_key(
        model_key,
        dispatch=True,
        dtype=dtype,
        distributed_config=DistributedConfig(tp_size=tp_size),
        **kwargs,
    )
    model._module.requires_grad_(False)
    return model


def seed_ranks(seed: int) -> None:
    """Put every rank's RNG in the same state.

    Not hygiene — a correctness requirement. If sampling diverges the ranks
    generate different tokens, and the model's own all-reduces then sum
    activations computed from different sequences, so the output is wrong on
    every rank rather than merely inconsistent. Many checkpoints ship
    ``do_sample: true`` in their generation config, so this applies to requests
    that never asked to sample.
    """
    import torch

    torch.manual_seed(seed)


class AbortController:
    """A rendezvous every rank passes through, used to stop them together.

    Cancelling a tensor-parallel run cannot be done to one rank: killing rank 0's
    thread mid-collective leaves the others blocked in NCCL forever. So the ranks
    agree to stop, at a point they all reach in the same order — the root
    module's forward, i.e. once per forward pass (once per token while
    generating).

    Rank 0 owns the decision and broadcasts one byte; every rank raises
    :class:`AbortedError` on the same iteration and unwinds its own forward,
    leaving the process group intact and reusable.

    A checkpoint costs one small broadcast plus the sync to read it — tens of
    microseconds, and once per forward rather than per layer, which is why it is
    installed here and not deeper. It bounds abort latency to a single forward
    pass; a rank wedged in Python (a runaway block) never reaches a checkpoint
    at all, which is what the caller's hard-kill fallback is for.

    ``armed`` must be flipped by every rank around the same request, or one rank
    broadcasts while the others do not and the group deadlocks — which is why
    arming rides the request protocol rather than being left permanently on.
    """

    def __init__(self, module: Any, source: bool) -> None:
        import torch

        self.source = source
        self.requested = False
        self.armed = False
        self._flag = torch.zeros(1, dtype=torch.uint8, device=torch.cuda.current_device())
        self._handle = module.register_forward_pre_hook(self._checkpoint)

    def _checkpoint(self, module: Any, args: Any) -> None:
        import torch.distributed as dist

        if not self.armed or not dist.is_initialized():
            return None

        if self.source:
            self._flag.fill_(1 if self.requested else 0)
        dist.broadcast(self._flag, src=0)
        if bool(self._flag.item()):
            raise AbortedError("the run was aborted")
        return None

    def arm(self) -> None:
        self.requested = False
        self.armed = True

    def disarm(self) -> None:
        self.armed = False
        self.requested = False

    def request(self) -> None:
        """Ask every rank to stop at its next checkpoint (rank 0 only)."""
        self.requested = True

    def close(self) -> None:
        self._handle.remove()
