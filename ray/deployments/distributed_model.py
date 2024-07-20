import gc
import logging
import socket
from datetime import timedelta
from typing import Dict

import ray
import torch
import torch.distributed
from pymongo import MongoClient
from ray import serve
from ray.serve import Application
from transformers import PreTrainedModel

from nnsight.models.mixins import RemoteableMixin
from nnsight.schema.Request import RequestModel

from ...schema.Response import ResponseModel, ResultModel
from ..distributed.parallel_dims import ParallelDims
from ..distributed.tensor_parallelism import parallelize_model
from ..distributed.util import load_hf_model_from_cache
from .model import ModelDeploymentArgs


@serve.deployment(ray_actor_options={"num_gpus": 1})
class DistributedModelDeployment:
    def __init__(
        self,
        model_key: str,
        api_url: str,
        database_url: str,
        torch_distributed_address: str,
        torch_distributed_port: int,
        torch_distributed_world_size: int,
        torch_distributed_world_rank: int,
        torch_distributed_world_timeout_seconds: int,
        data_parallelism_size: int,
        tensor_parallelism_size: int,
        pipeline_parallelism_size: int,
    ):

        self.model_key = model_key
        self.api_url = api_url
        self.database_url = database_url
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

        self.model = RemoteableMixin.from_model_key(self.model_key)

        self.head = torch_distributed_world_rank == 0

        if self.head:

            self.db_connection = MongoClient(self.database_url)

            if self.torch_distributed_address is None:

                ip_address = ray.get_runtime_context().worker.node_ip_address

                self.torch_distributed_address = (
                    f"tcp://{ip_address}:{self.torch_distributed_port}"
                )

            self.worker_applications = []

            for worker_world_rank in range(1, self.torch_distributed_world_size):

                worker_application = DistributedModelDeployment.bind(
                    model_key=self.model_key,
                    api_url=self.api_url,
                    database_url=self.database_url,
                    torch_distributed_address=self.torch_distributed_address,
                    torch_distributed_port=self.torch_distributed_port,
                    torch_distributed_world_size=self.torch_distributed_world_size,
                    torch_distributed_world_rank=worker_world_rank,
                    torch_distributed_world_timeout_seconds=self.torch_distributed_world_timeout_seconds,
                )

                self.worker_applications.append(worker_application)

        self.device = torch.get_default_device()

        torch.distributed.init_process_group(
            "nccl",
            timeout=timedelta(self.torch_distributed_world_timeout_seconds),
            world_size=self.torch_distributed_world_size,
            rank=torch_distributed_world_rank,
            device_id=self.device,
        )

        parallel_dims = ParallelDims(
            dp=self.data_parallelism_size,
            tp=self.tensor_parallelism_size,
            pp=self.pipeline_parallelism_size,
            world_size=self.torch_distributed_world_size,
            enable_loss_parallel=False,
        )

        world_mesh = parallel_dims.build_mesh(device_type=f"cuda")

        parallelize_model(self.model._model, "llama3", world_mesh["tp"])

        load_hf_model_from_cache(
            self.model._model, self.model._model.config._name_or_path
        )

        self.model._dispatched = True

        self.logger = logging.getLogger(__name__)

    def __call__(self, request: RequestModel):

        try:

            if self.head:

                for worker_application in self.worker_applications:

                    worker_application.remote(request)

            # Deserialize request
            obj = request.deserialize(self.model)

            # Execute object.
            local_result = obj.local_backend_execute()

            if self.head:

                ResponseModel(
                    id=request.id,
                    session_id=request.session_id,
                    received=request.received,
                    status=ResponseModel.JobStatus.COMPLETED,
                    description="Your job has been completed.",
                    result=ResultModel(
                        id=request.id,
                        value=obj.remote_backend_postprocess_result(local_result),
                    ),
                ).log(self.logger).save(self.db_connection).blocking_response(
                    self.api_url
                )

        except Exception as exception:

            if self.head:

                ResponseModel(
                    id=request.id,
                    session_id=request.session_id,
                    received=request.received,
                    status=ResponseModel.JobStatus.ERROR,
                    description=str(exception),
                ).log(self.logger).save(self.db_connection).blocking_response(
                    self.api_url
                )

        del request
        del local_result

        self.model._model.zero_grad()

        gc.collect()

        torch.cuda.empty_cache()

    async def status(self):

        model: PreTrainedModel = self.model._model

        return model.config.to_json_string()

    # Ray checks this method and restarts replica if it raises an exception
    def check_health(self):
        torch.cuda.synchronize()


class DistributedModelDeploymentArgs(ModelDeploymentArgs):

    torch_distributed_address: str = None
    torch_distributed_port: int = None
    torch_distributed_world_size: int
    torch_distributed_world_rank: int
    torch_distributed_world_timeout_seconds: int

    data_parallelism_size: int = 1
    tensor_parallelism_size: int = 1
    pipeline_parallelism_size: int = 1


def app(args: ModelDeploymentArgs) -> Application:
    return DistributedModelDeployment.bind(**args.model_dump())
