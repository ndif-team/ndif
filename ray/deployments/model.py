import os

from ray import serve

from nnsight.tracing.graph import Graph
from ..util import set_cuda_env_var
from .base import BaseModelDeployment, BaseModelDeploymentArgs, threaded


class ThreadedModelDeployment(BaseModelDeployment):

    @threaded
    def execute(self, graph: Graph):
        return super().execute(graph)


@serve.deployment(
    ray_actor_options={
        "num_cpus": 2,
    },
    max_ongoing_requests=200, max_queued_requests=200,
    health_check_period_s=10000000000000000000000000000000,
    health_check_timeout_s=12000000000000000000000000000000,
)
class ModelDeployment(ThreadedModelDeployment):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        config = {
            "config_string": self.model._model.config.to_json_string(),
            "repo_id": self.model._model.config._name_or_path,
        }
        
        serve.get_app_handle("Controller").set_model_configuration.remote(self.replica_context.app_name, config)


def app(args: BaseModelDeploymentArgs) -> serve.Application:

    return ModelDeployment.bind(**args.model_dump())
