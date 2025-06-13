import os

import ray
from ray import serve

from nnsight.schema.request import RequestModel

from .base import BaseModelDeployment, BaseModelDeploymentArgs


@ray.remote(num_cpus=0, num_gpus=0)
class ModelActor(BaseModelDeployment):

    pass
@serve.deployment(
    ray_actor_options={
        "num_cpus": 1,
    },
    max_ongoing_requests=200, max_queued_requests=200,
    health_check_period_s=10000000000000000000000000000000,
    health_check_timeout_s=12000000000000000000000000000000,
)
class ModelDeployment:
    
    def __init__(self, node_name:str, cached:bool, model_key:str, **kwargs):
        
        super().__init__()

        self.cached = cached
        self.model_key = model_key
        self.node_name = node_name
        
        self.cuda_devices = os.environ["CUDA_VISIBLE_DEVICES"]
                
        if self.cached:
            self.model_actor = ray.get_actor(f"ModelActor:{self.model_key}")
            ray.get(self.model_actor.from_cache.remote(self.cuda_devices))
        else:
            self.model_actor = ModelActor.options(
                name=f"ModelActor:{self.model_key}",
                resources={f"node:{self.node_name}": 0.01},
                lifetime="detached"
            ).remote(model_key=model_key, cuda_devices=self.cuda_devices, **kwargs)
            ray.get(self.model_actor.__ray_ready__.remote())
            
    def __call__(self, request: RequestModel):
        return self.model_actor.__call__.remote(request)
    
    def __del__(self):

        self.model_actor.to_cache.remote()

def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
