from datetime import timedelta
from typing import Any, Dict

import ray
import torch
import torch.distributed
from ray import serve
from ray.serve import Application

from ...schema import BackendRequestModel
from ..distributed.parallel_dims import ParallelDims
from ..distributed.tensor_parallelism import parallelize_model
from ..distributed.util import (
    load_hf_model_from_cache,
    patch_intervention_protocol,
)
from .base import BaseModelDeployment, BaseModelDeploymentArgs


@serve.deployment(
    ray_actor_options={"num_gpus": 1, "num_cpus": 2},
    health_check_period_s=10000000000000000000000000000000,
    health_check_timeout_s=12000000000000000000000000000000,
)
class ModelDeployment(BaseModelDeployment):
    def __init__(
        self,
        torch_distributed_address: str,
        torch_distributed_port: int,
        torch_distributed_world_size: int,
        torch_distributed_world_rank: int,
        torch_distributed_world_timeout_seconds: int,
        data_parallelism_size: int,
        tensor_parallelism_size: int,
        pipeline_parallelism_size: int,
        *args,
        **kwargs,
    ):

        super().__init__(
            *args,
            extra_kwargs={"meta_buffers": False, "patch_llama_scan": False},
            **kwargs,
        )

        self.replica_context = serve.get_replica_context()

        self.torch_distributed_address = torch_distributed_address
        self.torch_distributed_port = torch_distributed_port
        self.torch_distributed_world_size = torch_distributed_world_size
        self.torch_distributed_world_rank = torch_distributed_world_rank
        self.torch_distributed_world_timeout_seconds = (
            torch_distributed_world_timeout_seconds
        )
        self.data_parallelism_size = data_parallelism_size
        self.tensor_parallelism_size = tensor_parallelism_size
        self.pipeline_parallelism_size = pipeline_parallelism_size

        # Patches nnsight intervention protocol to handle DTensors.
        patch_intervention_protocol()

        self.head = torch_distributed_world_rank == 0

        if self.head:

            print("Initializing distributed head...")

            if self.torch_distributed_address is None:

                ip_address = ray.get_runtime_context().worker.node_ip_address
                self.torch_distributed_address = (
                    f"tcp://{ip_address}:{self.torch_distributed_port}"
                )

            print(
                f"=> Torch distributed address: {self.torch_distributed_address}"
            )

            self.worker_deployments = []

            for worker_world_rank in range(
                1, self.torch_distributed_world_size
            ):

                distributed_model_deployment_args = DistributedModelDeploymentArgs(
                    model_key=self.model_key,
                    api_url=self.api_url,
                    object_store_url=self.object_store_url,
                    object_store_access_key=self.object_store_access_key,
                    object_store_secret_key=self.object_store_secret_key,
                    torch_distributed_address=self.torch_distributed_address,
                    torch_distributed_world_size=self.torch_distributed_world_size,
                    torch_distributed_world_rank=worker_world_rank,
                    torch_distributed_world_timeout_seconds=self.torch_distributed_world_timeout_seconds,
                    tensor_parallelism_size=self.tensor_parallelism_size,
                    data_parallelism_size=self.data_parallelism_size,
                    pipeline_parallelism_size=self.pipeline_parallelism_size,
                )

                print(f"=> Binding distributed worker: {worker_world_rank}...")

                worker_application = ModelDeployment.bind(
                    **distributed_model_deployment_args.model_dump()
                )

                print(f"=> Bound distributed worker: {worker_world_rank}.")
                print(f"=> Serving distributed worker: {worker_world_rank}...")

                worker_deployment = serve._run(
                    worker_application,
                    _blocking=False,
                    name=f"Shard-{worker_world_rank}:{self.replica_context.app_name}",
                    route_prefix=f"/{self.replica_context.app_name}-{worker_world_rank}",
                )

                print(f"=> Served distributed worker: {worker_world_rank}.")

                self.worker_deployments.append(worker_deployment)

            print(f"Initialized distributed head.")

        self.init_distributed()

    def init_distributed(self):

        print(
            f"Initializing distributed worker: {self.torch_distributed_world_rank}. Ray address: {self.torch_distributed_address}..."
        )

        self.device = torch.device("cuda:0")

        torch.distributed.init_process_group(
            "nccl",
            init_method=self.torch_distributed_address,
            timeout=timedelta(self.torch_distributed_world_timeout_seconds),
            world_size=self.torch_distributed_world_size,
            rank=self.torch_distributed_world_rank,
            device_id=self.device,
        )

        print(
            f"Initialized distributed worker: {self.torch_distributed_world_rank}."
        )

        parallel_dims = ParallelDims(
            dp=self.data_parallelism_size,
            tp=self.tensor_parallelism_size,
            pp=self.pipeline_parallelism_size,
            world_size=self.torch_distributed_world_size,
            enable_loss_parallel=False,
        )

        world_mesh = parallel_dims.build_mesh(device_type=f"cuda")

        torch.set_default_device(self.device)

        print(
            f"Parallelizing distributed worker: {self.torch_distributed_world_rank}..."
        )

        parallelize_model(
            self.model._model,
            self.model._model.config._name_or_path,
            world_mesh["tp"],
        )

        print(
            f"Parallelized distributed worker: {self.torch_distributed_world_rank}."
        )
        print(
            f"Loading model for distributed worker: {self.torch_distributed_world_rank}..."
        )

        load_hf_model_from_cache(
            self.model._model, self.model._model.config._name_or_path
        )

        # Handle buffers
        self.model._model = self.model._model.to(self.device)

        self.model._model.requires_grad_(False)

        print(
            f"Loaded model for distributed worker: {self.torch_distributed_world_rank}."
        )

        self.model._dispatched = True

        torch.cuda.empty_cache()

    def pre(self, request: BackendRequestModel):
        if self.head:
            super().pre(request)

            for worker_deployment in self.worker_deployments:

                result = worker_deployment.remote(request)

        torch.distributed.barrier()

    def post(self, *args, **kwargs):
        if self.head:
            super().post(*args, **kwargs)

    def exception(self, *args, **kwargs):
        if self.head:
            super().exception(*args, **kwargs)

    def log(self, *args, **kwargs):
        if self.head:
            super().log(*args, **kwargs)

    def stream_send(self, *args, **kwargs):
        if self.head:
            super().stream_send(*args, **kwargs)


class DistributedModelDeploymentArgs(BaseModelDeploymentArgs):

    device_map: str | None = None
    dispatch: bool = False

    torch_distributed_address: str = None
    torch_distributed_port: int = None
    torch_distributed_world_rank: int = 0

    torch_distributed_world_size: int
    torch_distributed_world_timeout_seconds: int

    data_parallelism_size: int = 1
    tensor_parallelism_size: int = 1
    pipeline_parallelism_size: int = 1


def app(args: DistributedModelDeploymentArgs) -> Application:
    return ModelDeployment.bind(**args.model_dump())
