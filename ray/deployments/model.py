from ray import serve

from .base import BaseModelDeployment, BaseModelDeploymentArgs


@serve.deployment(
    ray_actor_options={
        "num_cpus": 2,
    },
    health_check_timeout_s=1200,
)
class ModelDeployment(BaseModelDeployment):
   pass

def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
