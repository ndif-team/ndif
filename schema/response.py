from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Optional

import requests
import socketio
from minio import Minio
from pydantic import field_serializer
from typing_extensions import Self

from nnsight.schema.response import ResponseModel

from ..metrics import RequestStatusGauge
from .mixins import ObjectStorageMixin, TelemetryMixin

if TYPE_CHECKING:
    from . import BackendRequestModel


class BackendResponseModel(ResponseModel, ObjectStorageMixin, TelemetryMixin):

    _bucket_name: ClassVar[str] = "responses"

    def __str__(self) -> str:
        return f"{self.id} - {self.status.name}: {self.description}"

    @property
    def blocking(self) -> bool:
        return self.session_id is not None

    def respond(self, sio: socketio.SimpleClient, object_store: Minio) -> ResponseModel:
        if self.blocking:

            fn = sio.client.emit

            if (
                self.status == ResponseModel.JobStatus.COMPLETED
                or self.status == ResponseModel.JobStatus.ERROR
                or self.status == ResponseModel.JobStatus.NNSIGHT_ERROR
            ):

                fn = sio.client.call

            fn("blocking_response", data=(self.session_id, self.pickle()))
        else:
            self.save(object_store)

        return self

    @field_serializer("status")
    def sstatus(self, value, _info):
        return value.value

    @field_serializer("received")
    def sreceived(self, value, _info):
        return str(value)

    def update_gauge(
        self,
        request: "BackendRequestModel",
    ) -> Self:
        """Updates the telemetry gauge to track the status of a request or response.

        Args:

        - gauge (NDIFGauge): Telemetry Gauge.
        - request (RequestModel): user request.
        - status (ResponseModel.JobStatus): status of the user request.
        - kwargs: key word arguments to NDIFGauge.update().

        Returns:
            Self.
        """

        RequestStatusGauge.update(request, self)

        return self
