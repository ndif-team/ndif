import logging
from typing import Dict, Union

import torch

from nnsight.modeling.mixins import RemoteableMixin

from ... import MODEL_KEY

logger = logging.getLogger("ndif")


class CacheEntry:
    def __init__(self, size_in_bytes: int, config: str):
        self.size_in_bytes = size_in_bytes
        self.config = config


class ModelEvaluator:

    def __init__(
        self,
        padding_factor: float = 0.15,
    ):

        self.padding_factor = padding_factor

        self.cache: Dict[MODEL_KEY, CacheEntry] = {}

        torch.set_default_dtype(torch.bfloat16)

    def __call__(self, model_key: MODEL_KEY) -> Union[float, Exception]:

        if model_key in self.cache:

            logger.info(
                f"=> Model {model_key} already in evaluation cache. Size: {self.cache[model_key].size_in_bytes}"
            )

            return self.cache[model_key].size_in_bytes

        try:

            meta_model = RemoteableMixin.from_model_key(
                model_key,
                dispatch=False,
                torch_dtype=torch.bfloat16,
            )

        except Exception as exception:

            return exception

        param_size = 0
        for param in meta_model._model.parameters():
            param_size += param.nelement() * param.element_size()
        buffer_size = 0
        for buffer in meta_model._model.buffers():
            buffer_size += buffer.nelement() * buffer.element_size()

        model_size_bytes = param_size + buffer_size

        model_size_bytes += model_size_bytes * self.padding_factor

        self.cache[model_key] = CacheEntry(model_size_bytes, meta_model._model.config)

        logger.info(f"=> New model evaluated: {model_key} size: {model_size_bytes}")

        return model_size_bytes
