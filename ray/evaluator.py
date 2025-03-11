from nnsight.modeling.mixins import RemoteableMixin
import torch

from .schema import (
    ModelConfigurationSchema,
    BaseModelDeploymentArgs,
    DistributedModelDeploymentArgs,
)
from ray.serve.schema import RayActorOptionsSchema


class ModelEvaluator:

    def __init__(
        self,
        accelerator_bytes: float,
        num_accelerators_per_node: int,
        model_import_path: str,
        distributed_model_import_path: str,
        padding_factor: float = 0.15,
    ):

        self.accelerator_bytes = accelerator_bytes
        self.num_accelerators_per_node = num_accelerators_per_node
        self.model_import_path = model_import_path
        self.distributed_model_import_path = distributed_model_import_path
        self.padding_factor = padding_factor

        self.cache = {}

        torch.set_default_dtype(torch.bfloat16)

    def __call__(self, model_key: str) -> ModelConfigurationSchema:

        if model_key in self.cache:

            return self.cache[model_key]

        meta_model = RemoteableMixin.from_model_key(
            model_key,
            dispatch=False,
            torch_dtype=torch.bfloat16,
        )._model

        param_size = 0
        for param in meta_model.parameters():
            param_size += param.nelement() * param.element_size()
        buffer_size = 0
        for buffer in meta_model.buffers():
            buffer_size += buffer.nelement() * buffer.element_size()

        model_size_bytes = param_size + buffer_size

        model_size_bytes += model_size_bytes * self.padding_factor

        gpus_required = model_size_bytes / self.accelerator_bytes

        if gpus_required > 1:
            gpus_required = (gpus_required // 1) + 1

        if gpus_required <= self.num_accelerators_per_node:

            model_configuration = ModelConfigurationSchema(
                model_import_path=self.model_import_path,
                ray_actor_options=RayActorOptionsSchema(
                    num_gpus=gpus_required, num_cpus=1
                ).dict(),
                num_replicas=1,
                args=BaseModelDeploymentArgs(model_key=model_key),
            )

        else:

            model_configuration = ModelConfigurationSchema(
                model_import_path=self.distributed_model_import_path,
                ray_actor_options=RayActorOptionsSchema(num_gpus=1, num_cpus=1).dict(),
                num_replicas=1,
                args=DistributedModelDeploymentArgs(
                    model_key=model_key,
                    torch_distributed_world_size=gpus_required,
                    tensor_parallelism_size=gpus_required,
                ),
            )

        self.cache[model_key] = model_configuration

        return model_configuration
