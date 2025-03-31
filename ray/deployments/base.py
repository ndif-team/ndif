import gc
import json
import os
import sys
import time
import traceback
import weakref
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from functools import wraps
from typing import Any, Dict

import ray
import socketio
import torch
from minio import Minio
from pydantic import BaseModel, ConfigDict
from ray import serve
from torch.amp import autocast
from torch.cuda import max_memory_allocated, memory_allocated, reset_peak_memory_stats
from transformers import PreTrainedModel

from nnsight.intervention.contexts import RemoteContext
from nnsight.modeling.mixins import RemoteableMixin
from nnsight.schema.request import StreamValueModel
from nnsight.tracing.backends import Backend
from nnsight.tracing.graph import Graph
from nnsight.tracing.protocols import StopProtocol
from nnsight.util import NNsightError

from ...logging import load_logger
from ...metrics import GPUMemMetric, ExecutionTimeMetric
from ...schema import (
    RESULT,
    BackendRequestModel,
    BackendResponseModel,
    BackendResultModel,
)
from ..util import set_cuda_env_var
from . import protocols


class ExtractionBackend(Backend):

    def __call__(self, graph: Graph) -> RESULT:

        try:

            graph.nodes[-1].execute()

            result = BackendResultModel.from_graph(graph)

        except StopProtocol.StopException:

            result = BackendResultModel.from_graph(graph)

        finally:

            graph.nodes.clear()
            graph.stack.clear()

        return result


class BaseDeployment:

    def __init__(
        self,
        api_url: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
    ) -> None:

        super().__init__()

        self.api_url = api_url
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key

        self.object_store = Minio(
            self.object_store_url,
            access_key=self.object_store_access_key,
            secret_key=self.object_store_secret_key,
            secure=False,
        )

        self.sio = socketio.SimpleClient(reconnection_attempts=10)

        self.logger = load_logger(
            service_name=str(self.__class__), logger_name="ray.serve"
        )
        
        try:

            self.replica_context = serve.get_replica_context()
            
        except:
            
            self.replica_context = None


class BaseDeploymentArgs(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    api_url: str
    object_store_url: str
    object_store_access_key: str
    object_store_secret_key: str


def threaded(method, size: int = 1):

    group = ThreadPoolExecutor(size)

    @wraps(method)
    def inner(*args, **kwargs):

        return group.submit(method, *args, **kwargs)

    return inner
