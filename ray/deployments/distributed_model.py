from datetime import timedelta
from typing import Any, Dict

import ray
import torch
import torch.distributed
from ray import serve
from ray.serve import Application

from nnsight.tracing.graph import Graph

from ...schema import (
    RESULT,
    BackendRequestModel,
    BackendResponseModel,
    BackendResultModel,
)
from ..distributed.parallel_dims import ParallelDims
from ..distributed.tensor_parallelism import parallelize_model
from ..distributed.util import load_hf_model_from_cache, patch_intervention_protocol
from ..util import NNsightTimer
from .base import BaseModelDeployment, BaseModelDeploymentArgs


class _ModelDeployment(BaseModelDeployment):
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

            print(f"=> Torch distributed address: {self.torch_distributed_address}")

            self.worker_actors = []

            for worker_world_rank in range(1, self.torch_distributed_world_size):

                name = f"Shard-{worker_world_rank}:{self.replica_context.app_name}"

                try:

                    serve.delete(name)

                    print(f"Found existing shard deployment {name}. Deleting...")

                except:
                    pass

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
                    execution_timeout=self.execution_timeout,
                )

                print(f"=> Creating distributed worker: {worker_world_rank}...")

                worker_actor = ModelDeploymentShard.options(name=name).remote(
                    **distributed_model_deployment_args.model_dump()
                )

                self.worker_actors.append(worker_actor)

                print(f"=> Created distributed worker: {worker_world_rank}.")

            print(f"Initialized distributed head.")

        self.init_distributed()

        self.timer = NNsightTimer(self.execution_timeout)

    def init_process_group(self):

        print("Initializing torch.distributed process group...")

        torch.distributed.init_process_group(
            "nccl",
            init_method=self.torch_distributed_address,
            timeout=timedelta(seconds=self.torch_distributed_world_timeout_seconds),
            world_size=self.torch_distributed_world_size,
            rank=self.torch_distributed_world_rank,
            device_id=self.device,
        )

        print("Initialized torch.distributed process group.")

    def init_distributed(self):

        print(
            f"Initializing distributed worker: {self.torch_distributed_world_rank}. Ray address: {self.torch_distributed_address}..."
        )

        self.device = torch.device("cuda:0")

        self.init_process_group()

        print(f"Initialized distributed worker: {self.torch_distributed_world_rank}.")

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

        print(f"Parallelized distributed worker: {self.torch_distributed_world_rank}.")
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

        self.model.dispatched = True

        torch.cuda.empty_cache()

        if self.head:

            config = {
                "config_json_string": self.model._model.config.to_json_string(),
                "repo_id": self.model._model.config._name_or_path,
            }

            serve.get_app_handle("Controller").set_model_configuration.remote(
                self.replica_context.app_name, config
            )

    def execute(self, graph: Graph):

        with self.timer:

            return super().execute(graph)

    def pre(self) -> Graph:

        graph = self.request.deserialize(self.model)

        if self.head:

            self.respond(
                status=BackendResponseModel.JobStatus.RUNNING,
                description="Your job has started running.",
            )

            for worker_deployment in self.worker_actors:

                worker_deployment.remote(self.request)

        torch.distributed.barrier()

        return graph

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

    def cleanup(self):

        torch.distributed.barrier()

        super().cleanup()


@serve.deployment(
    ray_actor_options={"num_gpus": 1, "num_cpus": 2},
    health_check_period_s=10000000000000000000000000000000,
    health_check_timeout_s=12000000000000000000000000000000,
    max_ongoing_requests=200,
    max_queued_requests=200,
)
class ModelDeployment(_ModelDeployment):
    pass


@ray.remote(num_cpus=2, num_gpus=1)
class ModelDeploymentShard(_ModelDeployment):
    pass


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
