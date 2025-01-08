from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Optional, Union

import ray
from fastapi import Request
from pydantic import ConfigDict
from typing_extensions import Self

from nnsight.schema.request import RequestModel
from nnsight.schema.response import ResponseModel
from nnsight.tracing.graph import Graph
from nnsight import NNsight
from .mixins import ObjectStorageMixin
from .response import BackendResponseModel

if TYPE_CHECKING:
    from .metrics import NDIFGauge


class BackendRequestModel(ObjectStorageMixin):
    """
    
    Attributes:
        - model_config:
        - graph (Union[bytes, ray.ObjectRef])
        - model_key:
        - session_id: 
        - format:
        - zlib:
        - id:
        - received:
        - api_key (str): api key associated with this request
        - _bucket_name:
        - _file_extension:
    
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, protected_namespaces=())

    _bucket_name: ClassVar[str] = "serialized-requests"
    _file_extension: ClassVar[str] = "json"

    graph: Union[bytes, ray.ObjectRef]

    model_key: str
    session_id: Optional[str] = None
    format: str
    zlib: bool

    id: str
    received: datetime

    api_key: str
    
    
    def deserialize(self, model: NNsight) -> Graph:
        
        graph = self.graph
        
        if isinstance(self.graph, ray.ObjectRef):

            graph = ray.get(graph)
        
        return RequestModel.deserialize(model, graph, 'json', self.zlib)

    @classmethod
    async def from_request(cls, request: Request, api_key: str, put: bool = True) -> Self:

        headers = request.headers

        graph = await request.body()

        if put:
            graph = ray.put(graph)

        return BackendRequestModel(
            graph=graph,
            model_key=headers["model_key"],
            session_id=headers.get('session_id', None),
            format=headers["format"],
            zlib=headers["zlib"],
            id=str(uuid.uuid4()),
            received=datetime.now(),
            api_key=api_key,
        )

    def create_response(
        self,
        status: ResponseModel.JobStatus,
        logger: logging.Logger,
        gauge: "NDIFGauge",
        description: str = "",
        data: bytes = None,
        gpu_mem: int = 0,
        msg: str = "",
    ) -> BackendResponseModel:
        """Generates a BackendResponseModel given a change in status to an ongoing request."""

        log_msg = f"{self.id} - {status.name}: {description}"

        response = (
            BackendResponseModel(
                id=self.id,
                session_id=self.session_id,
                received=self.received,
                status=status,
                description=description,
                data=data,
            )
            .backend_log(
                logger=logger,
                message=log_msg,
            )
            .update_gauge(
                gauge=gauge,
                request=self,
                status=status,
                api_key=self.api_key,
                gpu_mem=gpu_mem,
                msg=description,
            )
        )

        return response
