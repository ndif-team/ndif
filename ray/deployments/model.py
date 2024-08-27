import gc
import logging
import os
from functools import partial
from typing import Any, Dict

import torch
from pydantic import BaseModel
from pymongo import MongoClient
from ray import serve
from ray.serve import Application
from transformers import PreTrainedModel

from nnsight.models.mixins import RemoteableMixin
from nnsight.schema.Request import RequestModel

from ...schema.Response import ResponseModel, ResultModel

from ..util import set_cuda_env_var, update_nnsight_print_function
from logger import load_logger
from gauge import NDIFGauge


@serve.deployment()
class ModelDeployment:
    def __init__(self, model_key: str, api_url: str, database_url: str):

        set_cuda_env_var()

        # Set attrs
        self.model_key = model_key
        self.api_url = api_url
        self.database_url = database_url

        # Load and dispatch model based on model key.
        # The class returned could be any model type.
        # Device_map = auto means even distribute parmeaters across all gpus
        self.model = RemoteableMixin.from_model_key(
            self.model_key, device_map="auto", dispatch=True
        )
        # Make model weights non trainable / no grad.
        self.model._model.requires_grad_(False)

        # Clear cuda cache after model load.
        torch.cuda.empty_cache()

        # Init DB connection.
        self.db_connection = MongoClient(self.database_url)

        self.logger = load_logger(service_name="ray_model", logger_name="ray.serve")
        self.running = False
        self.gauge = NDIFGauge(service='ray')

    def __call__(self, request: RequestModel):

        self.gauge.update(request=request, api_key=' ', status=ResponseModel.JobStatus.RUNNING)

        # Send RUNNING response.
        ResponseModel(
            id=request.id,
            session_id=request.session_id,
            received=request.received,
            status=ResponseModel.JobStatus.RUNNING,
            description="Your job has started running.",
        ).log(self.logger).save(self.db_connection).blocking_response(
            self.api_url
        )
        
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

            self.running = True

            # Deserialize request
            obj = request.deserialize(self.model)

            # Execute object.
            local_result = obj.local_backend_execute()

            self.gauge.update(request=request, api_key=' ', status=ResponseModel.JobStatus.COMPLETED)

            # Send COMPELTED response.
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

            self.gauge.update(request=request, api_key=' ', status=ResponseModel.JobStatus.ERROR)

            ResponseModel(
                id=request.id,
                session_id=request.session_id,
                received=request.received,
                status=ResponseModel.JobStatus.ERROR,
                description=str(exception),
            ).log(self.logger).save(self.db_connection).blocking_response(
                self.api_url
            )

        finally:

            self.running = False

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
        ).log(self.logger).save(self.db_connection).blocking_response(
            self.api_url
        )

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
    database_url: str


def app(args: ModelDeploymentArgs) -> Application:

    return ModelDeployment.bind(**args.model_dump())
