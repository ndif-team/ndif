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

from nnsight.modeling.mixins.remotable import Remotable, bytes_per_element

from ......common.types import MODEL_KEY

logger = logging.getLogger("ndif.controller")

# The actor class a multi-GPU replica gets when its model can be split. Anything
# else multi-GPU falls back to the controller's default, which spreads whole
# layers over the cards with accelerate instead. Overridable per cluster
# (NDIF_TP_MODEL_ACTOR_CLASS) so an operator can point it at a subclass, or at
# the ordinary actor to turn tensor parallelism off entirely.
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
        tp_model_actor_class: str = TP_MODEL_ACTOR_CLASS,
    ):
        self.padding_factor = padding_factor
        self.padding_bias = padding_bias
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

        base_size_bytes = Remotable.estimate_bytes(
            model_key, resolved, trust_remote_code=trust_remote_code
        )
        max_tp = Remotable.max_tp_size(model_key, trust_remote_code=trust_remote_code)
        config = Remotable.checkpoint_config(
            model_key, trust_remote_code=trust_remote_code
        )
        revision = Remotable.checkpoint_revision(model_key)

        entry = CacheEntry(
            base_size_bytes,
            round(base_size_bytes / bytes_per_element(resolved)),
            config,
            revision,
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
    ) -> Union[float, Exception]:
        """Return the padded byte size for ``model_key`` (or the Exception on failure).

        ``dtype`` must match what the actor will load the model in, since element
        size drives the estimate; None -> bfloat16. ``trust_remote_code`` must
        likewise match, as repo code can change the architecture.
        """
        effective_padding = (
            padding_factor if padding_factor is not None else self.padding_factor
        )

        try:
            entry = self._entry(model_key, dtype, trust_remote_code)
        except Exception as exception:
            return exception

        padded_size = math.ceil(
            entry.base_size_in_bytes
            + entry.base_size_in_bytes * effective_padding
            + self.padding_bias
        )

        logger.debug(
            f"=> Model {model_key} size: {padded_size} "
            f"(padding_factor: {effective_padding}, padding_bias: {self.padding_bias})"
        )

        return padded_size

    def max_tp(
        self,
        model_key: MODEL_KEY,
        dtype: Any = None,
        trust_remote_code: bool = False,
    ) -> Optional[int]:
        """The largest tensor-parallel degree this model supports, or None.

        Best-effort: a model whose split can't be worked out is simply one that
        won't be placed tensor-parallel, which is the safe direction.
        """
        try:
            return self._entry(model_key, dtype, trust_remote_code).max_tp
        except Exception:
            logger.debug(f"Could not determine a TP degree for {model_key}", exc_info=True)
            return None

    def actor_class(
        self,
        model_key: MODEL_KEY,
        gpus: int,
        default: Any,
        dtype: Any = None,
        trust_remote_code: bool = False,
    ) -> Any:
        """The actor class to serve this replica with, given the cards it got.

        Tensor parallelism only when there is more than one card *and* the model
        splits evenly into exactly that many. Everything else — a single card, a
        model with no sharding plan, a model whose degree doesn't reach the count
        it needs — gets the default, which spreads whole layers with accelerate.
        """
        if gpus < 2:
            return default

        limit = self.max_tp(model_key, dtype, trust_remote_code)
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
