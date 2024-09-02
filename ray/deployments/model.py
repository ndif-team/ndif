import gc
import logging
import os
from functools import partial
from typing import Any, Dict

import torch
from minio import Minio
from pydantic import BaseModel
from ray import serve
from ray.serve import Application
from torch.amp import autocast
from transformers import PreTrainedModel

from nnsight.models.mixins import RemoteableMixin
from nnsight.schema.Request import RequestModel

from ...schema.Response import ResponseModel, ResultModel

from ..util import set_cuda_env_var, update_nnsight_print_function
from ...logging import load_logger


@serve.deployment()
class ModelDeployment:
    def __init__(
        self,
        model_key: str,
        api_url: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
    ):

        set_cuda_env_var()

        # Set attrs
        self.model_key = model_key
        self.api_url = api_url
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key

        torch.set_default_dtype(torch.bfloat16)

        # Load and dispatch model based on model key.
        # The class returned could be any model type.
        # Device_map = auto means even distribute parmeaters across all gpus
        self.model = RemoteableMixin.from_model_key(
            self.model_key,
            device_map="auto",
            dispatch=True,
            torch_dtype=torch.bfloat16,
        )
        # Make model weights non trainable / no grad.
        self.model._model.requires_grad_(False)

        # Clear cuda cache after model load.
        torch.cuda.empty_cache()

        # Init DB connection.
        self.object_store = Minio(
            self.object_store_url,
            access_key=self.object_store_access_key,
            secret_key=self.object_store_secret_key,
            secure=False,
        )

        self.logger = load_logger(service_name="ray.model", logger_name="ray.serve")
        self.running = False

    def __call__(self, request: RequestModel):

        # Send RUNNING response.
        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            status=ResponseModel.JobStatus.RUNNING,
            description="Your job has started running.",
        ).log(self.logger).respond(self.api_url, self.object_store)

        local_result = None

        try:

            # Changes the nnsight intervention graph function to respond via the ResponseModel instead of printing.
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

            with autocast(device_type="cuda", dtype=torch.get_default_dtype()):

                # Deserialize request
                obj = request.deserialize(self.model)

                # Execute object.
                local_result = obj.local_backend_execute()

            ResultModel(
                id=request.id,
                value=obj.remote_backend_postprocess_result(local_result),
            ).save(self.object_store)

            # Send COMPELTED response.
            ResponseModel(
                id=request.id,
                session_id=request.session_id,
                received=request.received,
                status=ResponseModel.JobStatus.COMPLETED,
                description="Your job has been completed.",
            ).log(self.logger).respond(self.api_url, self.object_store)

        except Exception as exception:

            ResponseModel(
                id=request.id,
                session_id=request.session_id,
                received=request.received,
                status=ResponseModel.JobStatus.ERROR,
                description=str(exception),
            ).log(self.logger).respond(self.api_url, self.object_store)

        del request
        del local_result

        self.model._model.zero_grad()

        gc.collect()

        torch.cuda.empty_cache()

    def log_to_user(self, data: Any, params: Dict[str, Any]):

        ResponseModel(
            **params,
            status=ResponseModel.JobStatus.LOG,
            description=str(data),
        ).log(self.logger).respond(self.api_url, self.object_store)

    async def status(self):

        model: PreTrainedModel = self.model._model

        return {
            "config_json_string": model.config.to_json_string(),
            "repo_id": model.config._name_or_path,
        }

    # # Ray checks this method and restarts replica if it raises an exception
    # def check_health(self):

    #     if not self.running:
    #         torch.cuda.empty_cache()

    def model_size(self) -> float:

        mem_params = sum(
            [
                param.nelement() * param.element_size()
                for param in self.model._model.parameters()
            ]
        )
        mem_bufs = sum(
            [
                buf.nelement() * buf.element_size()
                for buf in self.model._model.buffers()
            ]
        )
        mem_gbs = (mem_params + mem_bufs) * 1e-9

        return mem_gbs


class ModelDeploymentArgs(BaseModel):

    model_key: str
    api_url: str
    object_store_url: str
    object_store_access_key: str
    object_store_secret_key: str


def app(args: ModelDeploymentArgs) -> Application:

    return ModelDeployment.bind(**args.model_dump())
