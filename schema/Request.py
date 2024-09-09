from __future__ import annotations

import logging
from typing import ClassVar

from nnsight.schema.Request import RequestModel
from nnsight.schema.Response import ResponseModel

from .mixins import ObjectStorageMixin
from .Response import BackendResponseModel

class BackendRequestModel(RequestModel, ObjectStorageMixin):

    _bucket_name: ClassVar[str] = "serialized-requests"
    _file_extension: ClassVar[str] = "json"
    
    def create_response(self, status: ResponseModel.JobStatus, description: str, logger: logging.Logger, gauge: "NDIFGauge", gpu_mem: int = 0) -> BackendResponseModel:
        """Generates a BackendResponseModel given a change in status to an ongoing request."""

        msg = f"{self.id} - {status.name}: {description}"

        response = BackendResponseModel(
            id=self.id,
            session_id=self.session_id,
            received=self.received,
            status=status,
            description=description
        ).backend_log(
            logger=logger,
            message=msg,
        ).update_gauge(
            gauge=gauge, 
            request=self, 
            status=status, 
            api_key=" ",
            gpu_mem=gpu_mem,
        )

        return response