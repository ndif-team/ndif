"""
Mixins for schema classes providing object storage and telemetry functionality.

This module provides reusable mixins that can be composed with Pydantic models
to add S3 object storage capabilities and logging/telemetry features.
"""

from __future__ import annotations

import logging
import pickle
import types
from io import BytesIO
from typing import ClassVar, Tuple, Union

import torch
import zstandard as zstd
from botocore.response import StreamingBody
from pydantic import BaseModel, PrivateAttr
from typing_extensions import Self

from ..providers.objectstore import ObjectStoreProvider
from ..tracing import trace_span


class TensorStoragePickler(pickle.Pickler):
    """
    Custom pickler that ensures GPU tensors are moved to CPU before serialization.

    PyTorch tensors on GPU devices cannot be directly pickled for storage.
    This pickler intercepts tensor objects during serialization and transparently
    moves non-CPU tensors to CPU before pickling, ensuring compatibility with
    standard storage backends.

    Note:
        CPU tensors are left unchanged and handled by the default pickle mechanism.
    """

    def reducer_override(self, obj):
        """
        Override the reduction of objects during pickling.

        For GPU tensors, detach and move to CPU before reducing.
        All other objects use default pickle behavior.

        Args:
            obj: The object being pickled.

        Returns:
            The reduction tuple for GPU tensors, or NotImplemented for other objects.
        """
        if torch.is_tensor(obj):
            if obj.device.type != "cpu":
                cpu_tensor = obj.detach().to("cpu")
                return cpu_tensor.__reduce_ex__(pickle.HIGHEST_PROTOCOL)

            return NotImplemented

        return NotImplemented


# Create a custom pickle module that uses TensorStoragePickler by default.
# This allows torch.save() to use our custom pickler via the pickle_module parameter.
cpu_pickle_module = types.ModuleType("cpu_pickle_module")

for key, value in pickle.__dict__.items():
    setattr(cpu_pickle_module, key, value)

cpu_pickle_module.Pickler = TensorStoragePickler


class ObjectStorageMixin(BaseModel):
    """
    Mixin providing S3 object storage capabilities for Pydantic models.

    This mixin enables models to serialize themselves to and from an S3-compatible
    object store. It supports both JSON serialization (for simple models) and
    PyTorch serialization with optional zstd compression (for models containing tensors).

    Attributes:
        id: Unique identifier used as the object key in storage.
        _folder_name: Class-level folder/prefix for organizing objects in the bucket.
        _file_extension: Determines serialization format ('json' or 'pt').
        _size: Private attribute tracking the serialized size in bytes.

    Example:
        ```python
        class MyModel(ObjectStorageMixin):
            _folder_name = "my_models"
            _file_extension = "pt"

            id: str
            data: torch.Tensor

        model = MyModel(id="model-123", data=torch.randn(10))
        model.save(compress=True)

        loaded = MyModel.load("model-123")
        ```
    """

    id: str

    _folder_name: ClassVar[str] = "default"
    _file_extension: ClassVar[str] = "json"
    _size: int = PrivateAttr(default=0)

    @classmethod
    def object_name(cls, id: str) -> str:
        """
        Construct the full object key for storage.

        Args:
            id: The unique identifier for the object.

        Returns:
            The full object key in the format '{folder_name}/{id}.{extension}'.
        """
        return f"{cls._folder_name}/{id}.{cls._file_extension}"

    def url(self) -> str:
        """
        Generate a presigned URL for direct access to the stored object.

        Returns:
            A presigned URL valid for 2 hours that allows direct download.
        """
        client = ObjectStoreProvider.object_store

        return client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": ObjectStoreProvider.object_store_bucket,
                "Key": self.object_name(self.id),
            },
            ExpiresIn=3600 * 2,
        )

    def _save(self, data: BytesIO, content_type: str) -> None:
        """
        Internal method to upload serialized data to S3.

        Creates the bucket if it doesn't exist, then uploads the data.

        Args:
            data: BytesIO buffer containing the serialized object.
            content_type: MIME type for the uploaded object.
        """
        client = ObjectStoreProvider.object_store
        object_name = self.object_name(self.id)

        data.seek(0)

        # Ensure bucket exists
        try:
            client.head_bucket(Bucket=ObjectStoreProvider.object_store_bucket)
        except client.exceptions.ClientError:
            client.create_bucket(Bucket=ObjectStoreProvider.object_store_bucket)

        client.upload_fileobj(
            Fileobj=data,
            Bucket=ObjectStoreProvider.object_store_bucket,
            Key=object_name,
            ExtraArgs={"ContentType": content_type},
        )

    @classmethod
    def _load(
        cls, id: str, stream: bool = False
    ) -> Union[Tuple[StreamingBody, int], bytes]:
        """
        Internal method to download object data from S3.

        Args:
            id: The unique identifier of the object to load.
            stream: If True, return a streaming response instead of reading all data.

        Returns:
            If stream=True: Tuple of (StreamingBody, content_length).
            If stream=False: The complete object data as bytes.
        """
        client = ObjectStoreProvider.object_store
        object_name = cls.object_name(id)

        response = client.get_object(
            Bucket=ObjectStoreProvider.object_store_bucket, Key=object_name
        )

        if stream:
            return response["Body"], response["ContentLength"]

        data = response["Body"].read()
        response["Body"].close()

        return data

    def save(self, compress: bool = False) -> Self:
        """
        Serialize and save the object to S3 storage.

        The serialization format depends on the _file_extension class attribute:
        - 'json': Uses Pydantic's model_dump_json() for JSON serialization.
        - 'pt': Uses torch.save() with custom CPU tensor handling.

        Args:
            compress: If True and using 'pt' format, apply zstd compression (level 6).

        Returns:
            Self for method chaining.
        """
        with trace_span("objectstore.save", attributes={
            "ndif.objectstore.id": str(self.id),
            "ndif.objectstore.folder": self._folder_name,
            "ndif.objectstore.format": self._file_extension,
            "ndif.objectstore.compress": compress,
        }) as span:
            if self._file_extension == "json":
                data = BytesIO(self.model_dump_json().encode("utf-8"))
                content_type = "application/json"

            elif self._file_extension == "pt":
                data = BytesIO()

                # Extract all instance attributes, excluding metadata
                payload = {**self.__dict__, **self.model_extra}
                payload.pop("id", None)
                payload.pop("_size", None)

                # Use custom pickle module to handle GPU tensors
                torch.save(payload, data, pickle_module=cpu_pickle_module)

                if compress:
                    cctx = zstd.ZstdCompressor(level=6)
                    compressed = BytesIO()

                    with cctx.stream_writer(compressed, closefd=False) as writer:
                        data.seek(0)
                        while chunk := data.read(64 * 1024):
                            writer.write(chunk)

                    data = compressed

                content_type = "application/octet-stream"

            self._size = data.getbuffer().nbytes
            span.set_attribute("ndif.objectstore.size_bytes", self._size)
            self._save(data, content_type)

            return self

    @classmethod
    def load(
        cls, id: str, stream: bool = False
    ) -> Union[Tuple[StreamingBody, int], Self, dict]:
        """
        Load and deserialize an object from S3 storage.

        Args:
            id: The unique identifier of the object to load.
            stream: If True, return raw streaming response instead of deserializing.

        Returns:
            If stream=True: Tuple of (StreamingBody, content_length).
            If 'json' format: Deserialized model instance.
            If 'pt' format: Dictionary of the stored payload.
        """
        with trace_span("objectstore.load", attributes={
            "ndif.objectstore.id": str(id),
            "ndif.objectstore.folder": cls._folder_name,
            "ndif.objectstore.format": cls._file_extension,
            "ndif.objectstore.stream": stream,
        }) as span:
            object_data = cls._load(id, stream=stream)

            if stream:
                return object_data

            if cls._file_extension == "json":
                return cls.model_validate_json(object_data.decode("utf-8"))

            elif cls._file_extension == "pt":
                return torch.load(
                    BytesIO(object_data), map_location="cpu", weights_only=False
                )

    @classmethod
    def delete(cls, id: str) -> None:
        """
        Delete an object from S3 storage.

        Silently succeeds if the object doesn't exist.

        Args:
            id: The unique identifier of the object to delete.
        """
        client = ObjectStoreProvider.object_store
        object_name = cls.object_name(id)

        try:
            client.delete_object(
                Bucket=ObjectStoreProvider.object_store_bucket, Key=object_name
            )
        except Exception:
            pass


class TelemetryMixin:
    """
    Mixin providing logging utilities for schema classes.

    This mixin provides a convenient interface for logging messages at different
    severity levels while maintaining method chaining capabilities.
    """

    def backend_log(
        self, logger: logging.Logger, message: str, level: str = "info"
    ) -> Self:
        """
        Log a message using the provided logger.

        Args:
            logger: The logger instance to use.
            message: The message to log.
            level: Log level - one of 'info', 'error', or 'exception'.

        Returns:
            Self for method chaining.
        """
        if level == "info":
            logger.info(message)
        elif level == "error":
            logger.error(message)
        elif level == "exception":
            logger.exception(message)

        return self
