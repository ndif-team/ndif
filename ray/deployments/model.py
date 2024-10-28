import os

from ray import serve

from ...schema.Request import BackendRequestModel
from ..util import set_cuda_env_var
from .base import BaseModelDeployment, BaseModelDeploymentArgs, threaded


class ThreadedModelDeployment(BaseModelDeployment):

    @threaded
    def execute(self, request: BackendRequestModel):
        return super().execute(request)


@serve.deployment(
    ray_actor_options={
        "num_cpus": 2,
    },
    health_check_period_s=10000000000000000000000000000000,
    health_check_timeout_s=12000000000000000000000000000000,
)
class ModelDeployment(ThreadedModelDeployment):
    pass


def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
