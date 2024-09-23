from functools import partial, wraps
from typing import Any, Dict

import ray
import torch
from ray import serve
from torch.amp import autocast

from ...schema.Request import BackendRequestModel
from . import protocols
from .base import BaseModelDeployment, BaseModelDeploymentArgs, threaded
from ..util import set_cuda_env_var


class _ModelDeployment(BaseModelDeployment):
    
    def __init__(self, *args, **kwargs):
        
        super().__init__(*args, **kwargs)
        
        set_cuda_env_var()

    @threaded
    def __call__(self, request: BackendRequestModel) -> Any:

        try:
            request.object = ray.get(request.object)

            protocols.LogProtocol.put(partial(self.log, request=request))

            self.pre(request)

            with autocast(device_type="cuda", dtype=torch.get_default_dtype()):
                result = self.execute(request).result(self.execution_timeout)

            self.post(request, result)

        except Exception as e:

            self.exception(request, e)

        finally:

            del request
            del result

            self.cleanup()

    @threaded
    def execute(self, request: BackendRequestModel):

        return super().execute(request)


@serve.deployment(
    ray_actor_options={
        "num_cpus": 2,
    },
    health_check_timeout_s=1200,
)
class ModelDeployment(_ModelDeployment):
    pass


def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
