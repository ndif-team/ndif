from typing import TYPE_CHECKING

from . import Metric
import time

if TYPE_CHECKING:
    from ..schema import BackendRequestModel, BackendResponseModel


class RequestStatusTimeMetric(Metric):
    name: str = "request_status_time"

    @classmethod
    def update(
        cls,
        request: "BackendRequestModel",
        response: "BackendResponseModel",
    ) -> None:
        """
        Update the values of the gauge to reflect the current status of a request.
        Handles both Ray and Prometheus Gauge APIs.

        Args:
            - request (RequestModel): request object.
            - status (ResponseModel.JobStatus): user request job status.
            - api_key (str): user api key.
            - user_id (str):
            - msg (str): description of the current job status of the request.

        Returns:
        """

        _new_last_status_time = time.time()
        _last_status_time = request.last_status_time

        request.last_status_time = _new_last_status_time

        if _last_status_time is None:
            return

        time_delta = _new_last_status_time - _last_status_time

        super().update(
            time_delta,
            request_id=str(request.id),
            api_key=str(request.api_key),
            model_key=str(request.model_key),
            status=request.last_status.value,
        )
