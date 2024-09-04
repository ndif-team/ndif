from __future__ import annotations

import io
import logging
from io import BytesIO
from typing import Any, BinaryIO, ClassVar, Dict, List, Optional, Union

import requests
import torch
from minio import Minio
from pydantic import BaseModel, field_serializer
from typing_extensions import Self
from urllib3.response import HTTPResponse

from nnsight.schema.Request import RequestModel
from nnsight.schema.Response import ResponseModel as _ResponseModel
from nnsight.schema.Response import ResultModel as _ResultModel

from ..metrics import NDIFGauge


class StoredMixin(BaseModel):

    id: str
    _bucket_name: ClassVar[str] = "default"
    _file_extension: ClassVar[str] = "json"

    @classmethod
    def object_name(cls, id: str):

        return f"{id}.{cls._file_extension}"

    def _save(self, client: Minio, data: BytesIO, content_type: str, bucket_name: str = None) -> None:

        bucket_name = self._bucket_name if bucket_name is None else bucket_name
        object_name = self.object_name(self.id)

        data.seek(0)

        length = data.getbuffer().nbytes

        if not client.bucket_exists(bucket_name):
            client.make_bucket(bucket_name)

        client.put_object(
            bucket_name,
            object_name,
            data,
            length,
            content_type=content_type,
        )

    @classmethod
    def _load(
        cls, client: Minio, id: str, stream: bool = False
    ) -> HTTPResponse | bytes:

        bucket_name = cls._bucket_name
        object_name = cls.object_name(id)

        object = client.get_object(bucket_name, object_name)

        if stream:
            return object

        _object = object.read()

        object.close()

        return _object

    def save(self, client: Minio) -> Self:

        if self._file_extension == "json":

            data = BytesIO(self.model_dump_json().encode("utf-8"))

            content_type = "application/json"

        elif self._file_extension == "pt":

            data = BytesIO()

            torch.save(self.model_dump(), data)

            content_type = "application/octet-stream"

        self._save(client, data, content_type)

        return self

    @classmethod
    def load(cls, client: Minio, id: str, stream: bool = False) -> HTTPResponse | Self:

        object = cls._load(client, id, stream=stream)

        if stream:
            return object

        if cls._file_extension == "json":

            return cls.model_validate_json(object.decode("utf-8"))

        elif cls._file_extension == "pt":

            return torch.load(object, map_location="cpu", weights_only=False)

    @classmethod
    def delete(cls, client: Minio, id: str) -> None:

        bucket_name = cls._bucket_name
        object_name = cls.object_name(id)

        try:

            client.remove_object(bucket_name, id)

        except:
            pass


class ResultModel(_ResultModel, StoredMixin):

    _bucket_name: ClassVar[str] = "results"
    _file_extension: ClassVar[str] = "pt"


class ResponseModel(_ResponseModel, StoredMixin):

    _bucket_name: ClassVar[str] = "responses"

    def __str__(self) -> str:
        return f"{self.id} - {self.status.name}: {self.description}"

    def log(self, logger: logging.Logger) -> ResponseModel:
        if self.status == ResponseModel.JobStatus.ERROR:
            logger.exception(str(self))
        else:
            logger.info(str(self))

        return self

    def update_gauge(self, gauge: NDIFGauge, request: RequestModel, gpu_mem: int = 0) -> Self:
        gauge.update(request, " ", self.status, gpu_mem)
        return self

    def backup_request(self, client: Minio, request: RequestModel) -> Self:
        data = BytesIO(request.object.model_dump_json().encode("utf-8"))
        self._save(client, data, 'application/json', 'serialized-requests')

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
