from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar, Optional

import requests
from pydantic import field_serializer
from typing_extensions import Self

from nnsight.schema.response import ResponseModel

from ..metrics import RequestStatusTimeMetric
from ..providers.mailgun import MailgunProvider
from ..providers.objectstore import ObjectStoreProvider
from ..providers.socketio import SioProvider
from .mixins import ObjectStorageMixin, TelemetryMixin

if TYPE_CHECKING:
    from . import BackendRequestModel
    
def is_email(s):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w{2,}$'
    return re.match(pattern, s) is not None

class BackendResponseModel(ResponseModel, ObjectStorageMixin, TelemetryMixin):

    _bucket_name: ClassVar[str] = "responses"
    
    callback: Optional[str] = ''

    def __str__(self) -> str:
        return f"{self.id} - {self.status.name}: {self.description}"

    @property
    def blocking(self) -> bool:
        return self.session_id is not None

    def respond(self) -> ResponseModel:
        if self.blocking:

            fn = SioProvider.emit

            if (
                self.status == ResponseModel.JobStatus.COMPLETED
                or self.status == ResponseModel.JobStatus.ERROR
                or self.status == ResponseModel.JobStatus.NNSIGHT_ERROR
            ):

                fn = SioProvider.call

            fn("blocking_response", data=(self.session_id, self.pickle()))
        else:
            if self.callback != '':
                if is_email(self.callback):
                    if MailgunProvider.connected():
                        MailgunProvider.send_email(self.callback, f"NDIF Update For Job ID: {self.id}", self.model_dump_json(exclude_none=True, exclude_unset=True, exclude_defaults=True, exclude=["value"]))
                else:
                    callback_url = f"{self.callback}?status={self.status.value}&id={self.id}"
                    requests.get(callback_url)
            self.save(ObjectStoreProvider.object_store)

        return self

    @field_serializer("status")
    def sstatus(self, value, _info):
        return value.value

    def update_metric(
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

        RequestStatusTimeMetric.update(request, self)

        return self
