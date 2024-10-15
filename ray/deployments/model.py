from ray import serve

from ...schema.Request import BackendRequestModel

from .base import BaseModelDeployment, BaseModelDeploymentArgs, threaded

@serve.deployment(
    ray_actor_options={
        "num_cpus": 2,
    },
    health_check_period_s=10000000000000000000000000000000,
    health_check_timeout_s=12000000000000000000000000000000,
)
class ModelDeployment(BaseModelDeployment):
   pass

def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
