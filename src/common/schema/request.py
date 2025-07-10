from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, ClassVar, Coroutine, Optional, Union

import ray
from fastapi import Request
from pydantic import ConfigDict
from typing_extensions import Self

from nnsight import NNsight
from nnsight.schema.request import RequestModel
from nnsight.schema.response import ResponseModel
from nnsight.tracing.graph import Graph

from .mixins import ObjectStorageMixin
from .response import BackendResponseModel


class BackendRequestModel(ObjectStorageMixin):
    """

    Attributes:
        - model_config: model configuration.
        - graph (Union[bytes, ray.ObjectRef]): intervention graph object, could be in multiple forms.
        - model_key (str): model key name.
        - session_id (Optional[str]): connection session id.
        - format (str): format of the request body.
        - zlib (bool): is the request body compressed.
        - id (str): request id.
        - received (datetime.datetime): time of the request being received.
        - api_key (str): api key associated with this request.
        - _bucket_name (str): request result bucket storage name.
        - _file_extension (str): file extension.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, protected_namespaces=())

    _bucket_name: ClassVar[str] = "serialized-requests"
    _file_extension: ClassVar[str] = "json"

    _last_status: Optional[ResponseModel.JobStatus] = None

    graph: Optional[Union[Coroutine, bytes, ray.ObjectRef]] = None

    model_key: Optional[str] = None
    session_id: Optional[str] = None
    format: str
    zlib: Optional[bool] = True
    api_key: Optional[str] = ""

    id: str

    sent: Optional[float] = None

    def deserialize(self, model: NNsight) -> Graph:

        graph = self.graph

        if isinstance(self.graph, ray.ObjectRef):

            graph = ray.get(graph)

        return RequestModel.deserialize(model, graph, "json", self.zlib)

    @classmethod
    def from_request(cls, request: Request) -> Self:

        headers = request.headers

        sent = headers.get("sent-timestamp", None)

        if sent is not None:
            sent = float(sent)

        return BackendRequestModel(
            id=request.headers.get("ndif-request_id", str(uuid.uuid4())),
            graph=request.body(),
            model_key=headers.get("model_key", None),
            session_id=headers.get("session_id", None),
            format=headers.get("format", "json"),
            zlib=headers.get("zlib", True),
            sent=sent,
            api_key=headers.get("ndif-api-key", ""),
        )

    def create_response(
        self,
        status: ResponseModel.JobStatus,
        logger: logging.Logger,
        description: str = "",
        data: bytes = None,
    ) -> BackendResponseModel:
        """Generates a BackendResponseModel given a change in status to an ongoing request."""

        log_msg = f"{self.id} - {status.name}: {description}"

        logging_level = "info"

        if status == ResponseModel.JobStatus.ERROR:
            logging_level = "exception"
        elif status == ResponseModel.JobStatus.NNSIGHT_ERROR:
            logging_level = "exception"

        response = BackendResponseModel(
            id=self.id,
            session_id=self.session_id,
            status=status,
            description=description,
            data=data,
        ).backend_log(
            logger=logger,
            message=log_msg,
            level=logging_level,
        )

        if (
            status != self._last_status
            and status != ResponseModel.JobStatus.ERROR
            and status != ResponseModel.JobStatus.NNSIGHT_ERROR
        ):
            self._last_status = status
            response.update_metric(
                self,
            )

        return response
