from __future__ import annotations

import logging
import uuid
import zlib
from datetime import datetime
from io import BytesIO
from typing import ClassVar, Optional, Union

import msgspec
import ray
import torch
from fastapi import Request
from pydantic import ConfigDict
from typing_extensions import Self

from nnsight.schema.request import RequestModel
from nnsight.schema.response import ResponseModel
from nnsight.tracing.graph import Graph
from nnsight import NNsight
from .mixins import ObjectStorageMixin
from .response import BackendResponseModel


class BackendRequestModel(ObjectStorageMixin):

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

    @classmethod
    async def from_request(cls, request: Request, put: bool = True) -> Self:

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
        )

    def deserialize(self, model: NNsight) -> Graph:

        graph = self.graph

        if isinstance(self.graph, ray.ObjectRef):

            graph = ray.get(graph)

        if self.zlib:

            graph = zlib.decompress(graph)

        if self.format == "json":

            nnsight_request = msgspec.json.decode(graph)

            graph = RequestModel(**nnsight_request).deserialize(model)

        elif self.format == "pt":

            data = BytesIO(graph)

            data.seek(0)

            graph = torch.load(data, map_location="cpu", weights_only=False)

        return graph

    def create_response(
        self,
        status: ResponseModel.JobStatus,
        logger: logging.Logger,
        gauge: "NDIFGauge",
        description: str = None,
        data: bytes = None,
        gpu_mem: int = 0,
    ) -> BackendResponseModel:
        """Generates a BackendResponseModel given a change in status to an ongoing request."""

        msg = f"{self.id} - {status.name}: {description}"

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
                message=msg,
            )
            .update_gauge(
                gauge=gauge,
                request=self,
                status=status,
                api_key=" ",
                gpu_mem=gpu_mem,
            )
        )

        return response
