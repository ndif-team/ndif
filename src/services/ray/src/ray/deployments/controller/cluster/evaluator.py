import logging
from typing import Dict, Union, Any

import torch

from nnsight.modeling.mixins import RemoteableMixin

from .....types import MODEL_KEY

logger = logging.getLogger("ndif")


class CacheEntry:
    def __init__(self, base_size_in_bytes: int, n_params: int, config: str, revision: str):
        self.base_size_in_bytes = base_size_in_bytes
        self.n_params = n_params
        self.config = config
        self.revision = revision


class ModelEvaluator:
    def __init__(
        self,
        padding_factor: float = 0.15,
    ):
        self.padding_factor = padding_factor

        self.cache: Dict[MODEL_KEY, CacheEntry] = {}

        torch.set_default_dtype(torch.bfloat16)

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the evaluator."""

        return {
            "cache": {
                key: {
                    "base_size_in_bytes": value.base_size_in_bytes,
                    "config": value.config,
                }
                for key, value in self.cache.items()
            },
            "padding_factor": self.padding_factor,
            "dtype": str(torch.get_default_dtype()),
        }

    def __call__(self, model_key: MODEL_KEY, padding_factor: float | None = None) -> Union[float, Exception]:
        effective_padding = padding_factor if padding_factor is not None else self.padding_factor

        if model_key not in self.cache:
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

            base_size_bytes = param_size + buffer_size

            self.cache[model_key] = CacheEntry(
                base_size_bytes,
                n_params,
                meta_model._model.config,
                meta_model.revision,
            )

            logger.info(f"=> New model evaluated: {model_key} base_size: {base_size_bytes}")

        entry = self.cache[model_key]
        padded_size = entry.base_size_in_bytes + entry.base_size_in_bytes * effective_padding

        logger.info(f"=> Model {model_key} size: {padded_size} (padding_factor: {effective_padding})")

        return padded_size
