from __future__ import annotations

import logging
from io import BytesIO
from typing import ClassVar

import torch
from minio import Minio
from pydantic import BaseModel
from typing_extensions import Self
from urllib3.response import HTTPResponse

from nnsight.schema.Response import ResponseModel

class ObjectStorageMixin(BaseModel):
    """
    Mixin to provide object storage functionality for models using Minio.
    
    This mixin allows models to save and load themselves from an object store, 
    such as Minio, by serializing their data and interacting with the object storage API.

    Attributes:
        id (str): Unique identifier for the object to be stored.
        _bucket_name (ClassVar[str]): The default bucket name for storing objects.
        _file_extension (ClassVar[str]): The file extension used for stored objects.
        
    Methods:
        object_name(id: str) -> str:
            Returns the object name based on the provided ID and file extension.
        
        save(client: Minio) -> Self:
            Serializes and saves the object to the Minio storage.
        
        load(client: Minio, id: str, stream: bool = False) -> HTTPResponse | Self:
            Loads and deserializes the object from Minio storage.
        
        delete(client: Minio, id: str) -> None:
            Deletes the object from Minio storage.
    """
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

class TelemetryMixin:
    """
    Mixin to provide telemetry functionality for models, including logging and gauge updates.
    
    This mixin enables models to log their status and update Prometheus or Ray metrics (gauges)
    to track their state in the system. It abstracts the underlying telemetry mechanisms and 
    allows easy integration of logging and metric updates.

    Methods:
        backend_log(logger: logging.Logger, message: str, level: str = 'info') -> Self:
            Logs a message with the specified logging level (info, error, exception).

        update_gauge(gauge: NDIFGauge, request: RequestModel, status: ResponseModel.JobStatus, gpu_mem: int = 0) -> Self:
            Updates the telemetry gauge to track the status of a request or response.
    """
    def backend_log(self, logger: logging.Logger, message: str, level: str = 'info'):
        if level == 'info':
            logger.info(message)
        elif level == 'error':
            logger.error(message)
        elif level == 'exception':
            logger.exception(message)
        return self

    def update_gauge(self, gauge: "NDIFGauge", request: "BackendRequestModel", status: ResponseModel.JobStatus, api_key:str = " ", gpu_mem: int = 0):
        gauge.update(request, api_key, status, gpu_mem)
        return self