import logging
from typing import Dict, Any
from dataclasses import dataclass

import torch

from nnsight.modeling.mixins import RemoteableMixin

from .....types import MODEL_KEY

logger = logging.getLogger("ndif")


@dataclass
class CacheEntry:
    size_in_bytes: int
    n_params: int
    config: Any  # PretrainedConfig from transformers
    revision: str
    padding_factor: float


class GPUResourceEvaluator:
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
                    "size_in_bytes": value.size_in_bytes,
                    "config": value.config,
                    "padding_factor": value.padding_factor,
                }
                for key, value in self.cache.items()
            },
            "dtype": str(torch.get_default_dtype()),
        }

    def __call__(self, model_key: MODEL_KEY, padding_factor: float = 0.15) -> int | Exception:
        if model_key in self.cache:
            cached = self.cache[model_key]
            if cached.padding_factor == padding_factor:
                logger.info(
                    f"=> Model {model_key} already in evaluation cache. Size: {cached.size_in_bytes}"
                )
                return cached.size_in_bytes
            # padding_factor differs; fall through to recompute

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

        model_size_bytes = param_size + buffer_size
        model_size_bytes += int(model_size_bytes * padding_factor)

        self.cache[model_key] = CacheEntry(
            model_size_bytes,
            n_params,
            meta_model._model.config,
            str(meta_model.revision),
            padding_factor,
        )

        logger.info(f"=> New model evaluated: {model_key} size: {model_size_bytes}")

        return model_size_bytes
