from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Optional

import requests
from pydantic import field_serializer
from typing_extensions import Self

from nnsight.schema.response import ResponseModel

from ..providers.mailgun import MailgunProvider
from ..providers.socketio import SioProvider
from ..tracing import trace_span
from .mixins import ObjectStorageMixin, TelemetryMixin

if TYPE_CHECKING:
    from . import BackendRequestModel


def is_email(s):
    pattern = r"^[\w\.-]+@[\w\.-]+\.\w{2,}$"
    return re.match(pattern, s) is not None


class BackendResponseModel(ResponseModel, ObjectStorageMixin, TelemetryMixin):
    _folder_name: ClassVar[str] = "responses"

    callback: Optional[str] = ""

    def __str__(self) -> str:
        return f"{self.id} - {self.status.name}: {self.description}"

    @property
    def blocking(self) -> bool:
        return self.session_id is not None

    def respond(self) -> ResponseModel:
        with trace_span("response.deliver", attributes={
            "ndif.request.id": str(self.id),
            "ndif.response.status": self.status.name,
            "ndif.response.blocking": self.blocking,
        }) as span:
            if self.blocking:
                # Emit to socket manager - it will forward to the client (i.e. the user)
                span.set_attribute("ndif.response.delivery", "socketio")

                if self.status == ResponseModel.JobStatus.COMPLETED or self.status == ResponseModel.JobStatus.ERROR:
                    SioProvider.call("blocking_response", data=(self.session_id, self.pickle()), timeout=1)
                else:
                    SioProvider.emit("blocking_response", data=(self.session_id, self.pickle()))
            else:
                if self.callback != "":
                    if is_email(self.callback):
                        span.set_attribute("ndif.response.delivery", "email")
                        if MailgunProvider.connected():
                            MailgunProvider.send_email(
                                self.callback,
                                f"NDIF Update For Job ID: {self.id}",
                                self.model_dump_json(
                                    exclude_none=True,
                                    exclude_unset=True,
                                    exclude_defaults=True,
                                    exclude=["value"],
                                ),
                            )
                    else:
                        span.set_attribute("ndif.response.delivery", "callback_url")
                        callback_url = (
                            f"{self.callback}?status={self.status.value}&id={self.id}"
                        )
                        requests.get(callback_url)
                else:
                    span.set_attribute("ndif.response.delivery", "objectstore")

                if self.status != ResponseModel.JobStatus.LOG:
                    self.save()

            return self

    @field_serializer("status")
    def sstatus(self, value, _info):
        return value.value