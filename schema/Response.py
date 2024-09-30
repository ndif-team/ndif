from __future__ import annotations

from typing import ClassVar

import requests
from minio import Minio
from pydantic import field_serializer

from nnsight.schema.Response import ResponseModel

from .mixins import ObjectStorageMixin, TelemetryMixin

class BackendResponseModel(ResponseModel, ObjectStorageMixin, TelemetryMixin):
    """
    A model representing a backend response, inheriting from ResponseModel and mixing in
    ObjectStorageMixin and TelemetryMixin for additional functionality.
    """

    _bucket_name: ClassVar[str] = "responses"

    def __str__(self) -> str:
        """
        Returns a string representation of the BackendResponseModel.

        Returns:
            str: A formatted string containing the id, status name, and description.
        """
        return f"{self.id} - {self.status.name}: {self.description}"

    def blocking(self) -> bool:
        """
        Determines if the response is blocking based on the presence of a session_id.

        Returns:
            bool: True if the response is blocking (has a session_id), False otherwise.
        """
        return self.session_id is not None

    def respond(self, api_url: str, object_store: Minio) -> BackendResponseModel:
        """
        Sends the response either as a blocking response or saves it to object storage.

        Args:
            api_url (str): The URL of the API to send blocking responses to.
            object_store (Minio): The Minio object storage client for non-blocking responses.

        Returns:
            BackendResponseModel: The current instance of the response model.
        """
        if self.blocking():
            # Send a POST request for blocking responses
            requests.post(f"{api_url}/blocking_response", json=self.model_dump())
        else:
            # Save non-blocking responses to object storage
            self.save(object_store)

        return self

    @field_serializer("status")
    def sstatus(self, value, _info):
        """
        Serializes the status field.

        Args:
            value: The status value to serialize.
            _info: Additional serialization information (unused).

        Returns:
            The serialized value of the status.
        """
        return value.value

    @field_serializer("received")
    def sreceived(self, value, _info):
        """
        Serializes the received field.

        Args:
            value: The received value to serialize.
            _info: Additional serialization information (unused).

        Returns:
            str: The serialized value of received as a string.
        """
        return str(value)