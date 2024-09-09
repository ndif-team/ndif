from __future__ import annotations

from typing import ClassVar

from nnsight.schema.Request import RequestModel
from nnsight.schema.Response import ResponseModel

from .mixins import ObjectStorageMixin
from .Response import BackendResponseModel

class BackendRequestModel(RequestModel, ObjectStorageMixin):

    _bucket_name: ClassVar[str] = "serialized-requests"
    _file_extension: ClassVar[str] = "application/json"
    
    def create_response(self, status: ResponseModel.JobStatus, description: str) -> BackendResponseModel:
        """Generates a BackendResponseModel given a change in status to an ongoing request."""

        response = BackendResponseModel(
            id=self.id,
            session_id=self.session_id,
            received=self.received,
            status=status,
            description=description
        )
        return response

    def upgrade_from(self, request_model: RequestModel) -> BackendRequestModel:
        """Copy relevant fields from an existing RequestModel to the backend version."""
        for field, value in request_model.__dict__.items():
            setattr(self, field, value)
        return self