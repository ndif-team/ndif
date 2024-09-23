from typing import Any, Dict
from ray import serve
from torch import dtype
from torch._C import dtype

from ndif.schema.Request import BackendRequestModel

from .base import BaseModelDeployment, BaseModelDeploymentArgs, threaded


@serve.deployment(
    ray_actor_options={
        "num_cpus": 2,
    },
    health_check_timeout_s=1200,
)
class ModelDeployment(BaseModelDeployment):
    
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args,  **kwargs)
        
        self.super_execute = threaded(super().execute)
    
    @threaded
    def __call__(self, request: BackendRequestModel) -> Any:
        return super().__call__(request)
    
    def execute(self, request: BackendRequestModel):
        
        return self.super_execute(request).result(self.execution_timeout)  

def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
