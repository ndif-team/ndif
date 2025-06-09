import ray
import torch
from nnsight.modeling.mixins import RemoteableMixin

@ray.remote(num_cpus=1)
class CacheActor:
    
    def __init__(self):
        self.cache = {}
        
    async def get(self, model_key: str):
        return self.cache[model_key]
    
    async def cache(self, model_key: str, dtype: torch.dtype, **extra_kwargs):
        
        if model_key not in self.cache:
        
            self.cache[model_key] = ray.put(self.load(model_key, dtype, **extra_kwargs))
            
    def evict(self, model_key: str):
        del self.cache[model_key]  
        
    def load(self, model_key: str,  dtype: torch.dtype, **extra_kwargs):
        return RemoteableMixin.from_model_key(
            model_key,
            device_map="cpu",
            dispatch=True,
            torch_dtype=dtype,
            **extra_kwargs,
        )
    
