from dataclasses import dataclass
from typing import Dict

import ray
import torch
from ray.experimental.locations import get_local_object_locations

from nnsight.modeling.mixins import RemoteableMixin


@dataclass
class CacheEntry:
    object_ref: ray.ObjectRef
    size_bytes: int

@ray.remote(num_cpus=1)
class CacheActor:

    def __init__(self, total_memory_bytes: int):

        self.total_memory_bytes = total_memory_bytes
        self.available_memory_bytes = total_memory_bytes

        self.cache: Dict[str, CacheEntry] = {}

    async def get(self, model_key: str):
        return self.cache[model_key].object_ref

    async def cache(self, model_key: str, dtype: torch.dtype, **extra_kwargs):

        if model_key not in self.cache:

            object_ref = ray.put(self.load(model_key, dtype, **extra_kwargs))
            
            object_size = get_local_object_locations([object_ref])[object_ref]['object_size']

            self.cache[model_key] = CacheEntry(object_ref, object_size)
            
            self.available_memory_bytes -= object_size

    def evict(self, model_key: str):
        del self.cache[model_key]

    def load(self, model_key: str, dtype: torch.dtype, **extra_kwargs):
        return RemoteableMixin.from_model_key(
            model_key,
            device_map="cpu",
            dispatch=True,
            torch_dtype=dtype,
            **extra_kwargs,
        )
