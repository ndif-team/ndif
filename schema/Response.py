from __future__ import annotations

from typing import ClassVar

import requests
from minio import Minio
from pydantic import field_serializer

from nnsight.schema.Response import ResponseModel

from .mixins import ObjectStorageMixin, TelemetryMixin

class BackendResponseModel(ResponseModel, ObjectStorageMixin, TelemetryMixin):

    _bucket_name: ClassVar[str] = "responses"

    def __str__(self) -> str:
        return f"{self.id} - {self.status.name}: {self.description}"

    def blocking(self) -> bool:
        return self.session_id is not None

    def respond(self, api_url: str, object_store: Minio) -> ResponseModel:
        if self.blocking():
            requests.post(f"{api_url}/blocking_response", json=self.model_dump())
        else:
            self.save(object_store)

        return self

    @field_serializer("status")
    def sstatus(self, value, _info):
        return value.value

    @field_serializer("received")
    def sreceived(self, value, _info):
        return str(value)