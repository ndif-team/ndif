"""What the controller needs to know about a model before it places one.

Two questions, both answered from the model key alone, because a placement
decision has to be made before anything is loaded:

* **How much GPU memory?** — padded, since the estimate covers weights only.
* **Can it be split across cards, and how?** — which decides whether a
  multi-GPU replica runs tensor-parallel or spread layer-by-layer.

Neither is computed here. nnsight owns them (a wrapper class knows how to size
its own checkpoint — a HuggingFace one reads the parameter count off the Hub
rather than building the architecture to count it); this memoizes the answers
and turns them into the numbers placement works in.
"""

import logging
import math
from typing import Any, Dict, Optional, Union

import torch

from nnsight.modeling.huggingface import CheckpointUnreachable
from nnsight.modeling.mixins.remotable import Remotable, bytes_per_element

from ......common.types import MODEL_KEY

logger = logging.getLogger("ndif.controller")

# The actor class a multi-GPU replica gets when its model can be split, for a
# cluster that wants tensor parallelism. Not a default: it is the value to *set*
# NDIF_TP_MODEL_ACTOR_CLASS to. Leaving that unset disables tensor parallelism
# outright -- see `ModelEvaluator.tp_enabled`.
TP_MODEL_ACTOR_CLASS = "ndif.services.ray.tp.model.TPModelActor"


def tp_degree(limit: Optional[int], gpus: int) -> Optional[int]:
    """The tensor-parallel degree to use for ``gpus`` cards, or ``None``.

    ``limit`` is the largest degree the model supports; the degrees that work are
    its divisors, so this is the smallest divisor that is at least ``gpus``. A
    model needing 3 cards and shardable 8 ways runs at 4 — one card idle on the
    weights, but split evenly, which is the only way transformers will run it.

    ``None`` when the model can't be split, when one card is enough, or when no
    workable degree is that large: a model capped at 2 that needs 3 cards has to
    be spread some other way.
    """
    if limit is None or gpus < 2:
        return None
    for degree in range(gpus, limit + 1):
        if limit % degree == 0:
            return degree
    return None


class CacheEntry:
    def __init__(
        self,
        base_size_in_bytes: int,
        n_params: int,
        config: Any,
        revision: Optional[str],
        dtype: str,
        trust_remote_code: bool,
        max_tp: Optional[int],
    ):
        self.base_size_in_bytes = base_size_in_bytes
        # Reported by the controller's status. Derived from the size rather than
        # counted separately: the estimate *is* elements x bytes-per-element, so
        # dividing it back out is the same number the sizing used.
        self.n_params = n_params
        # The checkpoint's config, for the same status view. None for a wrapper
        # that has no notion of one.
        self.config = config
        # Which revision is deployed, so two replicas of one repo at different
        # revisions are tellable apart in the status.
        self.revision = revision
        # The dtype this estimate was computed for; a request at a different one
        # re-evaluates, since it is the whole difference between the numbers.
        self.dtype = dtype
        # Whether repo code ran; a different value can build a different
        # architecture, so it re-evaluates too.
        self.trust_remote_code = trust_remote_code
        # The largest tensor-parallel degree, or None if it can't be split.
        self.max_tp = max_tp


class ModelEvaluator:
    """Estimates a model's GPU footprint and how it can be split, memoized."""

    def __init__(
        self,
        padding_factor: float = 0.15,
        padding_bias: int = 0,
        tp_model_actor_class: Optional[str] = None,
    ):
        self.padding_factor = padding_factor
        self.padding_bias = padding_bias
        # None disables tensor parallelism entirely: no degree is worked out,
        # no GPU count is rounded up to one, and no replica is routed to the TP
        # actor. A cluster that has not said which actor serves a sharded model
        # has not opted in, and shouldn't be handed one.
        self.tp_model_actor_class = tp_model_actor_class

        self.cache: Dict[MODEL_KEY, CacheEntry] = {}

        torch.set_default_dtype(torch.bfloat16)

    def get_state(self) -> Dict[str, Any]:
        return {
            "cache": {
                key: {
                    "base_size_in_bytes": value.base_size_in_bytes,
                    "n_params": value.n_params,
                    "config": value.config,
                    "revision": value.revision,
                    "max_tp": value.max_tp,
                }
                for key, value in self.cache.items()
            },
            "padding_factor": self.padding_factor,
            "padding_bias": self.padding_bias,
            "tp_model_actor_class": self.tp_model_actor_class,
            "dtype": str(torch.get_default_dtype()),
        }

    def _entry(
        self, model_key: MODEL_KEY, dtype: Any, trust_remote_code: bool
    ) -> CacheEntry:
        """The memoized answers for this model, computing them if needed.

        Raises whatever nnsight raised: the caller turns a failure to evaluate
        into that model's deploy error.
        """
        resolved = _dtype_name(dtype)

        cached = self.cache.get(model_key)
        if (
            cached is not None
            and cached.dtype == resolved
            and cached.trust_remote_code == trust_remote_code
        ):
            return cached

        # One call for everything about the checkpoint itself — nnsight answers
        # them all from a single read of its metadata, where asking one at a time
        # fetched the same config twice.
        described = Remotable.describe_checkpoint(
            model_key, resolved, trust_remote_code=trust_remote_code
        )
        # Separately, because it is a question about the *runtime*, not the
        # files: the same weights shard eight ways under transformers tensor
        # parallelism and not at all under something else.
        max_tp = Remotable.max_tp_size(model_key, trust_remote_code=trust_remote_code)

        base_size_bytes = described.size_bytes
        entry = CacheEntry(
            base_size_bytes,
            # Derived only if the wrapper didn't say. The estimate *is* elements
            # times bytes-per-element, so dividing it back out is the same
            # number the sizing used — but a wrapper that knows the real count
            # should be believed over that.
            described.n_params
            if described.n_params is not None
            else round(base_size_bytes / bytes_per_element(resolved)),
            described.config,
            described.revision,
            resolved,
            trust_remote_code,
            max_tp,
        )
        self.cache[model_key] = entry

        logger.debug(
            f"=> New model evaluated: {model_key} base_size: {base_size_bytes} "
            f"(dtype {resolved}, max_tp {max_tp})"
        )
        return entry

    def __call__(
        self,
        model_key: MODEL_KEY,
        padding_factor: float | None = None,
        dtype: Any = None,
        trust_remote_code: bool = False,
        size_bytes: Optional[int] = None,
        padding_bias: Optional[int] = None,
    ) -> Union[float, Exception]:
        """Return the padded byte size for ``model_key`` (or the Exception on failure).

        ``dtype`` must match what the actor will load the model in, since element
        size drives the estimate; None -> bfloat16. ``trust_remote_code`` must
        likewise match, as repo code can change the architecture.

        ``size_bytes`` supplies the model's own weights directly — measured
        rather than estimated — and padding still applies on top. It also makes
        this the one placement input that needs no network: a deploy that names
        its own size goes through with the Hub unreachable, where an estimated
        one cannot. The description is still attempted, because the status reads
        the config and revision from it, but its failure is no longer fatal.
        """
        effective_padding = (
            padding_factor if padding_factor is not None else self.padding_factor
        )
        effective_bias = (
            padding_bias if padding_bias is not None else self.padding_bias
        )

        if size_bytes is None:
            try:
                base_size = self._entry(model_key, dtype, trust_remote_code).base_size_in_bytes
            except Exception as exception:
                return exception
        else:
            base_size = size_bytes
            try:
                self._entry(model_key, dtype, trust_remote_code)
            except Exception:
                # Only the description failed, and the size is what placement
                # needs. The status will show this model without a config.
                logger.warning(
                    f"=> Could not describe {model_key}, but it named its own "
                    "size — placing it anyway",
                    exc_info=True,
                )

        padded_size = math.ceil(
            base_size + base_size * effective_padding + effective_bias
        )

        logger.debug(
            f"=> Model {model_key} size: {padded_size} "
            f"(base: {base_size}{' given' if size_bytes is not None else ''}, "
            f"padding_factor: {effective_padding}, padding_bias: {effective_bias})"
        )

        return padded_size

    @property
    def tp_enabled(self) -> bool:
        """Whether this cluster serves tensor-parallel replicas at all.

        Off unless an operator named the actor class that serves one. That is a
        deliberate opt-in rather than a default: a tensor-parallel replica cannot
        be cached, is not sandboxed, and needs a transformers new enough to shard
        correctly, so a cluster should get one because somebody asked for it.
        """
        return self.tp_model_actor_class is not None

    def max_tp(
        self,
        model_key: MODEL_KEY,
        dtype: Any = None,
        trust_remote_code: bool = False,
        override: Optional[int] = None,
    ) -> Optional[int]:
        """The largest tensor-parallel degree this model supports, or None.

        ``None`` whenever this cluster has tensor parallelism switched off, so
        the placement that follows never rounds a GPU count up to a shardable
        degree — a model simply gets the cards its size needs.

        Best-effort: a model whose split can't be worked out is simply one that
        won't be placed tensor-parallel, which is the safe direction.

        The two ways of not knowing are logged differently on purpose. ``None``
        is also the answer meaning "this model genuinely cannot be split", so a
        checkpoint the Hub was too slow to describe would otherwise be recorded
        as unshardable and placed layer-by-layer, with a debug line as the only
        trace — indistinguishable, in the logs of a cluster that is running
        fine, from a model that really is unshardable.
        """
        if not self.tp_enabled:
            return None

        if override == 0:
            # "Do not place this tensor-parallel", spelled as a number so a
            # config can say it; None means "nobody said, ask the checkpoint".
            return None

        try:
            limit = self._entry(model_key, dtype, trust_remote_code).max_tp
        except CheckpointUnreachable as unreachable:
            if override is not None:
                # Nothing to check against, and the operator said a number.
                return override
            logger.warning(
                f"=> Could not reach the Hub to describe {model_key}: {unreachable}. "
                "Placing it without tensor parallelism; this is a network result, "
                "not a property of the model."
            )
            return None
        except Exception:
            logger.debug(f"Could not determine a TP degree for {model_key}", exc_info=True)
            return override

        if override is None:
            return limit

        if limit is None:
            # The checkpoint says it cannot be split. An override is an explicit
            # instruction, so it still wins — but say so, because if it is wrong
            # the failure lands at load with the cards already reserved.
            logger.warning(
                f"=> {model_key} reports no tensor-parallel plan, but max_tp="
                f"{override} was configured; taking the configured value"
            )
            return override

        # A cap, not a widening. Asking for more ways than the weights actually
        # divide into passes every check here — the degree looks workable and the
        # GPU count looks even — and then transformers refuses at load, after the
        # cards are reserved and the weights read across them. That is the
        # sequence this whole path exists to move earlier.
        if override > limit:
            logger.warning(
                f"=> {model_key} was configured max_tp={override} but its weights "
                f"divide at most {limit} ways; using {limit}"
            )
        return min(override, limit)

    def prefetch(
        self,
        model_key: MODEL_KEY,
        dtype: Any = None,
        trust_remote_code: bool = False,
    ) -> None:
        """Read everything about ``model_key`` into the cache, off the hot path.

        Every other method here is pure arithmetic over a cache entry *except*
        for the moment it builds one, which reaches the Hub. Placement runs on
        the controller's event loop, so that moment is the one thing on this path
        that can park `check_nodes`, every `_monitor_deployment`, and every RPC
        behind it — for as long as the network takes.

        Calling this from a worker thread first turns the entry cold-to-warm
        without holding the loop, and leaves the placement that follows doing
        arithmetic on a cache hit. Failures are deliberately swallowed: this is a
        warm-up, and whatever went wrong will happen again, in the same way, in
        the call that actually needs the answer and can report it properly.
        """
        try:
            self._entry(model_key, dtype, trust_remote_code)
        except Exception:
            logger.debug(f"Could not prefetch a description for {model_key}", exc_info=True)

    def actor_class(
        self,
        model_key: MODEL_KEY,
        gpus: int,
        default: Any,
        dtype: Any = None,
        trust_remote_code: bool = False,
        max_tp_override: Optional[int] = None,
    ) -> Any:
        """The actor class to serve this replica with, given the cards it got.

        Tensor parallelism only when there is more than one card *and* the model
        splits evenly into exactly that many. Everything else — a single card, a
        model with no sharding plan, a model whose degree doesn't reach the count
        it needs — gets the default, which spreads whole layers with accelerate.
        """
        if not self.tp_enabled or gpus < 2:
            return default

        limit = self.max_tp(model_key, dtype, trust_remote_code, max_tp_override)
        if tp_degree(limit, gpus) != gpus:
            return default

        logger.info(
            f"=> {model_key} placed tensor-parallel across {gpus} GPUs "
            f"(shards up to {limit})"
        )
        return self.tp_model_actor_class


def _dtype_name(dtype: Any) -> str:
    """The dtype as a plain name for nnsight's sizing.

    ``None`` means the cluster default. A ``torch.dtype`` is spelled back out so
    a caller passing an object and a caller passing ``"bfloat16"`` land on the
    same cache entry — and so a quantization name, which has no torch dtype,
    passes through untouched.
    """
    if dtype is None:
        return "bfloat16"
    if isinstance(dtype, torch.dtype):
        return str(dtype).removeprefix("torch.")
    return str(dtype)
