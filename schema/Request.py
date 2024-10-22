from __future__ import annotations

import logging
from typing import ClassVar

from ray import ObjectRef

from nnsight.schema.Request import OBJECT_TYPES, RequestModel
from nnsight.schema.Response import ResponseModel

from .mixins import ObjectStorageMixin
from .Response import BackendResponseModel


class BackendRequestModel(RequestModel, ObjectStorageMixin):

    _bucket_name: ClassVar[str] = "serialized-requests"
    _file_extension: ClassVar[str] = "json"

    object: ObjectRef | str | OBJECT_TYPES

    def create_response(
        self,
        status: ResponseModel.JobStatus,
        logger: logging.Logger,
        gauge: "NDIFGauge",
        description: str = None,
        data: bytes = None,
        gpu_mem: int = 0,
    ) -> BackendResponseModel:
        """Generates a BackendResponseModel given a change in status to an ongoing request."""

        msg = f"{self.id} - {status.name}: {description}"

        response = (
            BackendResponseModel(
                id=self.id,
                session_id=self.session_id,
                received=self.received,
                status=status,
                description=description,
                data=data
            )
            .backend_log(
                logger=logger,
                message=msg,
            )
            .update_gauge(
                gauge=gauge,
                request=self,
                status=status,
                api_key=" ",
                gpu_mem=gpu_mem,
            )
        )

        return response
