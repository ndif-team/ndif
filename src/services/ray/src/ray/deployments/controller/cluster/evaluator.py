from typing import Dict

import torch

from nnsight.modeling.mixins import RemoteableMixin

from ... import MODEL_KEY

from .....logging.logger import load_logger

LOGGER = load_logger("Controller")

class ModelEvaluator:

    def __init__(
        self,
        padding_factor: float = 0.15,
    ):

        self.padding_factor = padding_factor

        self.size_in_bytes_cache: Dict[MODEL_KEY, int] = {}
        self.config_cache: Dict[MODEL_KEY, str] = {}

        torch.set_default_dtype(torch.bfloat16)

    def __call__(self, model_key: MODEL_KEY) -> float:

        if model_key in self.size_in_bytes_cache:
            
            LOGGER.info(f"=> Evaluator: Model {model_key} already in cache. Size: {self.size_in_bytes_cache[model_key]}")

            return self.size_in_bytes_cache[model_key]

        meta_model = RemoteableMixin.from_model_key(
            model_key,
            dispatch=False,
            torch_dtype=torch.bfloat16,
        )._model

        param_size = 0
        for param in meta_model.parameters():
            param_size += param.nelement() * param.element_size()
        buffer_size = 0
        for buffer in meta_model.buffers():
            buffer_size += buffer.nelement() * buffer.element_size()

        model_size_bytes = param_size + buffer_size

        model_size_bytes += model_size_bytes * self.padding_factor

        self.size_in_bytes_cache[model_key] = model_size_bytes
        self.config_cache[model_key] = meta_model.config
        
        LOGGER.info(f"=> Evaluator: New model {model_key} size: {model_size_bytes}")
        
        return model_size_bytes