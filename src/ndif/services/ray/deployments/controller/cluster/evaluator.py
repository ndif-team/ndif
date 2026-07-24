import logging
import math
from typing import Any, Dict, Union

import torch

from nnsight.modeling.mixins.remotable import Remotable

from ......common.types import MODEL_KEY
from ...modeling.util import resolve_dtype

logger = logging.getLogger("ndif.controller")


class CacheEntry:
    def __init__(
        self,
        base_size_in_bytes: int,
        n_params: int,
        config: Any,
        revision: str,
        dtype: Any,
        trust_remote_code: bool,
    ):
        self.base_size_in_bytes = base_size_in_bytes
        self.n_params = n_params
        self.config = config
        self.revision = revision
        # The torch.dtype this estimate was computed for; a request at a
        # different dtype re-evaluates (element sizes differ).
        self.dtype = dtype
        # Whether repo code ran at load; a different value can build a different
        # architecture, so it re-evaluates too.
        self.trust_remote_code = trust_remote_code


class ModelEvaluator:
    """Estimates a model's GPU footprint, memoized per model_key.

    Loads the model on the meta device (no weights) via nnsight to count
    parameters/buffers, then pads to account for runtime overhead. The padded
    byte size drives placement decisions in the cluster.
    """

    def __init__(self, padding_factor: float = 0.15, padding_bias: int = 0):
        self.padding_factor = padding_factor
        self.padding_bias = padding_bias

        self.cache: Dict[MODEL_KEY, CacheEntry] = {}

        torch.set_default_dtype(torch.bfloat16)

    def get_state(self) -> Dict[str, Any]:
        return {
            "cache": {
                key: {
                    "base_size_in_bytes": value.base_size_in_bytes,
                    "config": value.config,
                }
                for key, value in self.cache.items()
            },
            "padding_factor": self.padding_factor,
            "padding_bias": self.padding_bias,
            "dtype": str(torch.get_default_dtype()),
        }

    def __call__(
        self,
        model_key: MODEL_KEY,
        padding_factor: float | None = None,
        dtype: Any = None,
        trust_remote_code: bool = False,
    ) -> Union[float, Exception]:
        """Return the padded byte size for ``model_key`` (or the Exception on failure).

        ``dtype`` (name/``torch.dtype``/None) must match the dtype the actor will
        load the model in, since element sizes drive the estimate; None ->
        bfloat16. ``trust_remote_code`` must likewise match the actor's load, as
        repo code can change the architecture. A cached estimate for a different
        dtype or trust_remote_code is recomputed.
        """
        effective_padding = (
            padding_factor if padding_factor is not None else self.padding_factor
        )
        resolved_dtype = resolve_dtype(dtype)

        cached = self.cache.get(model_key)
        if (
            cached is None
            or cached.dtype != resolved_dtype
            or cached.trust_remote_code != trust_remote_code
        ):
            try:
                # dispatch=False builds the architecture on the meta device (no
                # weights), enough to count parameters/buffers. nnsight wraps the
                # torch module as an Envoy; the module itself is ``_module``.
                meta_model = Remotable.from_model_key(
                    model_key,
                    dispatch=False,
                    torch_dtype=resolved_dtype,
                    trust_remote_code=trust_remote_code,
                )
            except Exception as exception:
                return exception

            module = meta_model._module

            param_size = 0
            n_params = 0
            for param in module.parameters():
                param_size += param.nelement() * param.element_size()
                n_params += param.nelement()
            buffer_size = 0
            for buffer in module.buffers():
                buffer_size += buffer.nelement() * buffer.element_size()

            base_size_bytes = param_size + buffer_size

            self.cache[model_key] = CacheEntry(
                base_size_bytes,
                n_params,
                module.config,
                meta_model.revision,
                resolved_dtype,
                trust_remote_code,
            )

            logger.debug(
                f"=> New model evaluated: {model_key} base_size: {base_size_bytes} "
                f"(dtype {resolved_dtype})"
            )

        entry = self.cache[model_key]
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
