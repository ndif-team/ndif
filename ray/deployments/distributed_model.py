import gc
import inspect
import logging
from datetime import timedelta
from functools import partial
from typing import Any, Dict

import ray
import torch
import torch.distributed
from minio import Minio
from ray import serve
from ray.serve import Application
from torch.cuda import max_memory_allocated, reset_peak_memory_stats
from torch.amp import autocast
from transformers import PreTrainedModel

from nnsight.models.mixins import RemoteableMixin
from nnsight.schema.Request import RequestModel

from ...schema.Response import ResponseModel, ResultModel
from ..distributed.parallel_dims import ParallelDims
from ..distributed.tensor_parallelism import parallelize_model
from ..distributed.util import (
    load_hf_model_from_cache,
    patch_intervention_protocol,
)
from ..util import update_nnsight_print_function
from .model import ModelDeploymentArgs
from ...logging import load_logger
from  ...metrics import NDIFGauge

@serve.deployment(
    ray_actor_options={"num_gpus": 1, "concurrency_groups": {"io": 5, "compute": 1}}, health_check_timeout_s=1200
)
class ModelDeployment:
    def __init__(
        self,
        model_key: str,
        api_url: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
        torch_distributed_address: str,
        torch_distributed_port: int,
        torch_distributed_world_size: int,
        torch_distributed_world_rank: int,
        torch_distributed_world_timeout_seconds: int,
        data_parallelism_size: int,
        tensor_parallelism_size: int,
        pipeline_parallelism_size: int,
    ):

        self.replica_context = serve.get_replica_context()

        self.model_key = model_key
        self.api_url = api_url
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key
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

        self.model = RemoteableMixin.from_model_key(
            self.model_key, meta_buffers=False, patch_llama_scan=False
        )

        # Patches nnsight intervention protocol to handle DTensors.
        patch_intervention_protocol()

        self.logger = load_logger(service_name=f"ray.distributed_model_{torch_distributed_world_rank}", logger_name="ray.serve")
        self.gauge = NDIFGauge(service='ray')
        self.head = torch_distributed_world_rank == 0

        if self.head:

            print("Initializing distributed head...")

            self.object_store = Minio(
                self.object_store_url,
                access_key=self.object_store_access_key,
                secret_key=self.object_store_secret_key,
                secure=False,
            )

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
        torch.set_default_dtype(torch.bfloat16)

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

    @ray.method(concurrency_group="compute")
    async def __call__(self, request: RequestModel):

        if self.head:

            ResponseModel(
                id=request.id,
                session_id=request.session_id,
                received=request.received,
                status=ResponseModel.JobStatus.RUNNING,
                description="Your job has started running.",
            ).log(self.logger).update_gauge(self.gauge, request).respond(self.api_url, self.object_store)
            update_nnsight_print_function(
                partial(
                    self.log_to_user,
                    params={
                        "id": request.id,
                        "session_id": request.session_id,
                        "received": request.received,
                    },
                )
            )

            for worker_deployment in self.worker_deployments:

                worker_deployment.remote(request)

        torch.distributed.barrier()

        local_result = None

        try:
            # For tracking peak GPU usage of current worker
            reset_peak_memory_stats()

            with autocast(device_type="cuda", dtype=torch.get_default_dtype()):

                # Deserialize request
                obj = request.deserialize(self.model)

            # Peak VRAM usage (in bytes) of current worker during execution
            gpu_mem = max_memory_allocated()

            if self.head:

                ResultModel(
                    id=request.id,
                    value=obj.remote_backend_postprocess_result(local_result),
                ).save(self.object_store)

                ResponseModel(
                    id=request.id,
                    session_id=request.session_id,
                    received=request.received,
                    status=ResponseModel.JobStatus.COMPLETED,
                    description="Your job has been completed.",
                ).log(self.logger).update_gauge(self.gauge, request, gpu_mem).respond(self.api_url, self.object_store)

        except Exception as exception:

            if self.head:

                ResponseModel(
                    id=request.id,
                    session_id=request.session_id,
                    received=request.received,
                    status=ResponseModel.JobStatus.ERROR,
                    description=str(exception),
                ).log(self.logger).update_gauge(self.gauge, request).respond(self.api_url, self.object_store)

        del request
        del local_result

        self.model._model.zero_grad()

        gc.collect()

        torch.cuda.empty_cache()

    # # Ray checks this method and restarts replica if it raises an exception
    # async def check_health(self):

    #     for device in range(torch.cuda.device_count()):
    #         torch.cuda.mem_get_info(device)

    def log_to_user(self, data: Any, params: Dict[str, Any]):

        ResponseModel(
            **params,
            status=ResponseModel.JobStatus.LOG,
            description=str(data),
        ).log(self.logger).respond(self.api_url, self.object_store)

    @ray.method(concurrency_group="io")
    async def status(self):

        model: PreTrainedModel = self.model._model

        return {
            "config_json_string": model.config.to_json_string(),
            "repo_id": model.config._name_or_path,
        }


class DistributedModelDeploymentArgs(ModelDeploymentArgs):

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
