from __future__ import annotations

import logging
from io import BytesIO
from typing import ClassVar, TYPE_CHECKING, Union, Any, Optional

import torch
import boto3
from botocore.response import StreamingBody
from pydantic import BaseModel
from typing_extensions import Self

if TYPE_CHECKING:
    from nnsight.schema.response import ResponseModel
    from nnsight.schema.request import RequestModel


class ObjectStorageMixin(BaseModel):
    """
    Mixin to provide object storage functionality for models using S3.
    
    This mixin allows models to save and load themselves from an S3 object store
    by serializing their data and interacting with the S3 API.

    Attributes:
        id (str): Unique identifier for the object to be stored.
        _bucket_name (ClassVar[str]): The default bucket name for storing objects.
        _file_extension (ClassVar[str]): The file extension used for stored objects.
        
    Methods:
        object_name(id: str) -> str:
            Returns the object name based on the provided ID and file extension.
        
        save(client: boto3.client) -> Self:
            Serializes and saves the object to S3 storage.
        
        load(client: boto3.client, id: str, stream: bool = False) -> StreamingBody | Self:
            Loads and deserializes the object from S3 storage.
        
        delete(client: boto3.client, id: str) -> None:
            Deletes the object from S3 storage.
    """
    id: str
    size: Optional[int] = None
    
    _bucket_name: ClassVar[str] = "default"
    _file_extension: ClassVar[str] = "json"
    

    @classmethod
    def object_name(cls, id: str):
        return f"{id}.{cls._file_extension}"

    def _save(self, client: boto3.client, data: BytesIO, content_type: str, bucket_name: str = None) -> None:
        bucket_name = self._bucket_name if bucket_name is None else bucket_name
        object_name = self.object_name(self.id)

        data.seek(0)

        # Check if bucket exists, create if it doesn't
        try:
            client.head_bucket(Bucket=bucket_name)
        except client.exceptions.ClientError:
            client.create_bucket(Bucket=bucket_name)

        # Upload object to S3
        client.upload_fileobj(
            Fileobj=data,
            Bucket=bucket_name,
            Key=object_name,
            ExtraArgs={'ContentType': content_type}
        )

    @classmethod
    def _load(
        cls, client: boto3.client, id: str, stream: bool = False
    ) -> Union[StreamingBody, bytes]:
        bucket_name = cls._bucket_name
        object_name = cls.object_name(id)

        response = client.get_object(Bucket=bucket_name, Key=object_name)
        
        if stream:
            return response['Body'], response['ContentLength']

        data = response['Body'].read()
        response['Body'].close()

        return data

    def save(self, client: boto3.client) -> Self:
        if self._file_extension == "json":
            data = BytesIO(self.model_dump_json().encode("utf-8"))
            content_type = "application/json"
        elif self._file_extension == "pt":
            data = BytesIO()
            torch.save(self.model_dump(), data)
            content_type = "application/octet-stream"
            
        self.size = data.getbuffer().nbytes

        self._save(client, data, content_type)
        
        return self

    @classmethod
    def load(cls, client: boto3.client, id: str, stream: bool = False) -> Union[StreamingBody, Self]:
        object_data = cls._load(client, id, stream=stream)

        if stream:
            return object_data

        if cls._file_extension == "json":
            return cls.model_validate_json(object_data.decode("utf-8"))
        elif cls._file_extension == "pt":
            return torch.load(BytesIO(object_data), map_location="cpu", weights_only=False)

    @classmethod
    def delete(cls, client: boto3.client, id: str) -> None:
        bucket_name = cls._bucket_name
        object_name = cls.object_name(id)

        try:
            client.delete_object(Bucket=bucket_name, Key=object_name)
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

        update_gauge(gauge: NDIFGauge) -> Self:
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

