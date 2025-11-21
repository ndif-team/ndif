from __future__ import annotations

import logging
import uuid
from typing import ClassVar, Coroutine, Optional, Union

import ray
from fastapi import Request
from pydantic import ConfigDict
from typing_extensions import Self

from nnsight import NNsight
from nnsight.schema.request import RequestModel
from nnsight.schema.response import ResponseModel

from ..types import API_KEY, MODEL_KEY, REQUEST_ID, SESSION_ID
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

    model_key: Optional[MODEL_KEY] = None
    session_id: Optional[SESSION_ID] = None
    zlib: Optional[bool] = True
    api_key: Optional[API_KEY] = ""
    callback: Optional[str] = ""
    hotswapping: Optional[bool] = False
    python_version: Optional[str] = ""
    nnsight_version: Optional[str] = ""
    content_length: Optional[int] = 0
    ip_address: Optional[str] = ""
    user_agent: Optional[str] = ""
    id: REQUEST_ID

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

        request_id = (
            uuid.uuid4()
            if headers.get("ndif-request_id") is None
            else headers.get("ndif-request_id")
        )

        return BackendRequestModel(
            id=str(request_id),
            request=request.body(),
            model_key=headers.get("nnsight-model-key", None),
            session_id=headers.get("ndif-session_id", None),
            zlib=headers.get("nnsight-zlib", True),
            last_status_time=sent,
            api_key=headers.get("ndif-api-key"),
            callback=headers.get("ndif-callback", ""),
            python_version=headers.get("python-version", ""),
            nnsight_version=headers.get("nnsight-version", ""),
            content_length=int(headers.get("content-length", 0)),
            ip_address=request.client.host if request.client else "",
            user_agent=headers.get("user-agent", ""),
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
            id=str(self.id),
            session_id=str(self.session_id) if self.session_id else None,
            status=status,
            description=description,
            data=data,
            callback=self.callback,
        ).backend_log(
            logger=logger,
            message=log_msg,
            level=logging_level,
        )

        logger.info(
            f"Request status: {status}, Last status: {self.last_status}, Last status time: {self.last_status_time}"
        )

        if status != self.last_status and status != ResponseModel.JobStatus.LOG:
            logger.info(f"Updating last status: {status}")
            self.last_status = status

            response.update_metric(
                self,
            )

        return response
