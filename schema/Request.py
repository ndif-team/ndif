from __future__ import annotations

import logging
from typing import ClassVar

from ray import ObjectRef

from nnsight.schema.Request import OBJECT_TYPES, RequestModel
from nnsight.schema.Response import ResponseModel

from .mixins import ObjectStorageMixin
from .Response import BackendResponseModel


class BackendRequestModel(RequestModel, ObjectStorageMixin):
    """
    A model representing a backend request, inheriting from RequestModel and ObjectStorageMixin.

    This class extends the base RequestModel with additional functionality for
    object storage and response creation.

    Attributes:
        _bucket_name (ClassVar[str]): The name of the bucket for storing serialized requests.
        _file_extension (ClassVar[str]): The file extension for stored request objects.
        object (ObjectRef | str | OBJECT_TYPES): The object associated with the request.
    """

    _bucket_name: ClassVar[str] = "serialized-requests"
    _file_extension: ClassVar[str] = "json"

    object: ObjectRef | str | OBJECT_TYPES

    def create_response(
        self,
        status: ResponseModel.JobStatus,
        description: str,
        logger: logging.Logger,
        gauge: "NDIFGauge",
        gpu_mem: int = 0,
    ) -> BackendResponseModel:
        """
        Generates a BackendResponseModel given a change in status to an ongoing request.

        This method creates a new BackendResponseModel instance, logs the status change,
        and updates the telemetry gauge.

        Args:
            status (ResponseModel.JobStatus): The current status of the job.
            description (str): A description of the current status or any additional information.
            logger (logging.Logger): The logger instance to use for logging the status change.
            gauge ("NDIFGauge"): The gauge instance to update with the new status.
            gpu_mem (int, optional): The current GPU memory usage. Defaults to 0.

        Returns:
            BackendResponseModel: A new response model instance with the updated status and information.
        """

        # Create a formatted message for logging
        msg = f"{self.id} - {status.name}: {description}"

        # Create and configure the response
        response = (
            BackendResponseModel(
                id=self.id,
                session_id=self.session_id,
                received=self.received,
                status=status,
                description=description,
            )
            .backend_log(
                logger=logger,
                message=msg,
            )
            .update_gauge(
                gauge=gauge,
                request=self,
                status=status,
                api_key=" ",  # Note: Empty string used as default API key
                gpu_mem=gpu_mem,
            )
        )

        return response