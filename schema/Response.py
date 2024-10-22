from __future__ import annotations

from typing import Any, ClassVar, Optional

import requests
import socketio
from minio import Minio
from pydantic import field_serializer

from nnsight.schema.Response import ResponseModel

from .mixins import ObjectStorageMixin, TelemetryMixin


class BackendResponseModel(ResponseModel, ObjectStorageMixin, TelemetryMixin):

    _bucket_name: ClassVar[str] = "responses"

    data: Optional[Any] = None

    def __str__(self) -> str:
        return f"{self.id} - {self.status.name}: {self.description}"

    def blocking(self) -> bool:
        return self.session_id is not None

    def respond(
        self, sio: socketio.SimpleClient, object_store: Minio
    ) -> ResponseModel:
        if self.blocking():

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
