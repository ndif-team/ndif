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

    last_status: Optional[ResponseModel.JobStatus] = None
    last_status_time: Optional[float] = None

    request: Optional[Union[Coroutine, bytes, ray.ObjectRef]] = None

    model_key: Optional[str] = None
    session_id: Optional[str] = None
    format: str
    zlib: bool
    
    callback: Optional[str] = ''

    id: str

    def deserialize(self, model: NNsight) -> RequestModel:

        request = self.request

        if isinstance(self.request, ray.ObjectRef):

            request = ray.get(request)

        return RequestModel.deserialize(model, request, self.zlib)

    @classmethod
    def from_request(cls, request: Request) -> Self:

        headers = request.headers
        
        sent = headers.get("ndif-timestamp", None)
        
        if sent is not None:
            sent = float(sent)

        return BackendRequestModel(
            graph=request.body(),
            model_key=headers["model_key"],
            session_id=headers.get("session_id", None),
            format=headers["format"],
            zlib=headers["zlib"],
            id=str(uuid.uuid4()),
            sent=float(headers.get("sent-timestamp", None)),
            api_key=api_key,
            callback=headers.get("callback", ''),
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
            

        response = (
            BackendResponseModel(
                id=self.id,
                session_id=self.session_id,
                status=status,
                description=description,
                data=data,
                callback=self.callback,
            )
            .backend_log(
                logger=logger,
                message=log_msg,
                level=logging_level,
            )
            .update_metric(
                self,
            )
            

        return response
