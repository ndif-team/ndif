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
    Mixin to provide object storage functionality for pydantic models using Minio.
    
    This mixin allows pydantic models to save and load themselves from an object store, 
    such as Minio, by serializing their data and interacting with the object storage API.

    Attributes:
        id (str): Unique identifier for the object to be stored.
        _bucket_name (ClassVar[str]): The default bucket name for storing objects.
        _file_extension (ClassVar[str]): The file extension used for stored objects.
    """
    id: str
    _bucket_name: ClassVar[str] = "default"
    _file_extension: ClassVar[str] = "json"

    @classmethod
    def object_name(cls, id: str):
        """
        Returns the object name based on the provided ID and file extension.

        Args:
            id (str): The unique identifier for the object.

        Returns:
            str: The full object name including the file extension.
        """
        return f"{id}.{cls._file_extension}"

    def _save(self, client: Minio, data: BytesIO, content_type: str, bucket_name: str = None) -> None:
        """
        Internal method to save data to Minio storage.

        Args:
            client (Minio): The Minio client instance.
            data (BytesIO): The data to be saved.
            content_type (str): The MIME type of the data.
            bucket_name (str, optional): The bucket name to save to. Defaults to self._bucket_name.
        """
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
        """
        Internal method to load data from Minio storage.

        Args:
            client (Minio): The Minio client instance.
            id (str): The unique identifier for the object.
            stream (bool, optional): Whether to return a stream or read the entire object. Defaults to False.

        Returns:
            HTTPResponse | bytes: The loaded object data.
        """
        bucket_name = cls._bucket_name
        object_name = cls.object_name(id)

        object = client.get_object(bucket_name, object_name)

        if stream:
            return object

        _object = object.read()

        object.close()

        return _object

    def save(self, client: Minio) -> Self:
        """
        Serializes and saves the object to the Minio storage.

        Args:
            client (Minio): The Minio client instance.

        Returns:
            Self: The instance of the class.
        """
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
        """
        Loads and deserializes the object from Minio storage.

        Args:
            client (Minio): The Minio client instance.
            id (str): The unique identifier for the object.
            stream (bool, optional): Whether to return a stream or read the entire object. Defaults to False.

        Returns:
            HTTPResponse | Self: The loaded and deserialized object.
        """
        object = cls._load(client, id, stream=stream)

        if stream:
            return object

        if cls._file_extension == "json":

            return cls.model_validate_json(object.decode("utf-8"))

        elif cls._file_extension == "pt":

            return torch.load(object, map_location="cpu", weights_only=False)

    @classmethod
    def delete(cls, client: Minio, id: str) -> None:
        """
        Deletes the object from Minio storage.

        Args:
            client (Minio): The Minio client instance.
            id (str): The unique identifier for the object to be deleted.
        """
        bucket_name = cls._bucket_name
        object_name = cls.object_name(id)

        try:
            client.remove_object(bucket_name, object_name)

        except:
            pass

class TelemetryMixin:
    """
    Mixin to provide telemetry functionality for models, including logging and gauge updates.
    
    This mixin enables models to log their status and update Prometheus or Ray metrics (gauges)
    to track their state in the system. It abstracts the underlying telemetry mechanisms and 
    allows easy integration of logging and metric updates.
    """
    def backend_log(self, logger: logging.Logger, message: str, level: str = 'info'):
        """
        Logs a message with the specified logging level.

        Args:
            logger (logging.Logger): The logger instance to use.
            message (str): The message to log.
            level (str, optional): The logging level ('info', 'error', or 'exception'). Defaults to 'info'.

        Returns:
            Self: The instance of the class.
        """
        if level == 'info':
            logger.info(message)
        elif level == 'error':
            logger.error(message)
        elif level == 'exception':
            logger.exception(message)
        return self

    def update_gauge(self, gauge: "NDIFGauge", request: "BackendRequestModel", status: ResponseModel.JobStatus, api_key:str = " ", gpu_mem: int = 0):
        """
        Updates the telemetry gauge to track the status of a request or response.

        Args:
            gauge (NDIFGauge): The gauge instance to update.
            request (BackendRequestModel): The request model associated with the update.
            status (ResponseModel.JobStatus): The current job status.
            api_key (str, optional): The API key associated with the request. Defaults to " ".
            gpu_mem (int, optional): The GPU memory usage. Defaults to 0.

        Returns:
            Self: The instance of the class.
        """
        gauge.update(
            request=request,
            api_key=api_key,
            status=status,
            gpu_mem=gpu_mem
        )
        return self