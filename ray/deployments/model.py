import os
from ray import serve

from ...schema.Request import BackendRequestModel

from .base import BaseModelDeployment, BaseModelDeploymentArgs, threaded
from ..util import set_cuda_env_var
class ThreadedModelDeployment(BaseModelDeployment):
    
    @threaded
    def execute(self, request: BackendRequestModel):
        return super().execute(request)


@serve.deployment(
    ray_actor_options={
        "num_cpus": 2,
    },
    health_check_timeout_s=1200,
)
class ModelDeployment(ThreadedModelDeployment):
   
   def __init__(self, *args, **kwargs):
       
        if os.environ.get("CUDA_VISIBLE_DEVICES","") == "":
            set_cuda_env_var()
       
        super().__init__(*args, **kwargs)

def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
