import logging
from typing import Dict, Any
from dataclasses import dataclass

import torch

from nnsight.modeling.mixins import RemoteableMixin

from .....types import MODEL_KEY

logger = logging.getLogger("ndif")


@dataclass
class CacheEntry:
    """Pretrained Config from transformers."""
    config: Any

    """Revision of the model."""
    revision: str

    """Number of parameters in the model."""
    n_params: int

    """Size of the model in bytes before padding."""
    size_in_bytes_pre_padding: int

    """Padding factor for the computed amount of GPU/CPU memory."""
    padding_factor: float


class ResourceEvaluator:
    def __init__(
        self,
    ):
        self.cache: Dict[MODEL_KEY, CacheEntry] = {}

        torch.set_default_dtype(torch.bfloat16)

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the evaluator."""

        return {
            "cache": {
                key: {
                    "size_in_bytes_pre_padding": value.size_in_bytes_pre_padding,
                    "config": value.config,
                    "padding_factor": value.padding_factor,
                }
                for key, value in self.cache.items()
            },
            "dtype": str(torch.get_default_dtype()),
        }

    def __call__(self, model_key: MODEL_KEY, padding_factor: float = 0.15) -> int | Exception:
        # Skip model size computation if it's already in the cache.
        if model_key in self.cache:
            logger.info(
                f"=> Model {model_key} already in evaluation cache. Pre-padding size: {self.cache[model_key].size_in_bytes_pre_padding}"
            )

            cached = self.cache[model_key]

            model_size_bytes_pre_padding = cached.size_in_bytes_pre_padding
            model_size_bytes = model_size_bytes_pre_padding + int(model_size_bytes_pre_padding * padding_factor)

            return model_size_bytes

        try:
            meta_model = RemoteableMixin.from_model_key(
                model_key,
                dispatch=False,
                torch_dtype=torch.bfloat16,
            )

        except Exception as exception:
            return exception

        param_size = 0
        n_params = 0
        for param in meta_model._model.parameters():
            param_size += param.nelement() * param.element_size()
            n_params += param.nelement()
        buffer_size = 0
        for buffer in meta_model._model.buffers():
            buffer_size += buffer.nelement() * buffer.element_size()

        model_size_bytes_pre_padding = param_size + buffer_size
        model_size_bytes = model_size_bytes_pre_padding + int(model_size_bytes_pre_padding * padding_factor)

        self.cache[model_key] = CacheEntry(
            config=meta_model._model.config,
            revision=str(meta_model.revision),
            n_params=n_params,
            size_in_bytes_pre_padding=model_size_bytes_pre_padding,
            padding_factor=padding_factor,
        )

        logger.info(f"=> New model evaluated: {model_key} size: {model_size_bytes}")

        return model_size_bytes
