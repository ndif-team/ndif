from enum import Enum
from typing import TYPE_CHECKING

from . import Metric

if TYPE_CHECKING:

    from ..schema import BackendRequestModel, BackendResponseModel


class RequestStatusMetric(Metric):
    """
    This class abstracts the usage of metrics for tracking the status of requests across different services.
    Specifically, it handles the complexity introduced by Ray's distributed system when using Prometheus.

    Considerations:
    - Ray's distributed nature complicates direct use of Prometheus client objects, requiring dynamic HTTP servers or a Pushgateway, which adds complexity and potential performance issues.
    - To avoid this, Ray's built-in metrics API (Gauge) is used, which handles the distributed aspect automatically.
    - However, Ray's API differs slightly from the Prometheus client, leading to a messier interface in this class.
    - Additionally, Ray prepends "ray_" to metric names, which needs to be handled separately in Grafana.

    This class supports both Ray's Gauge API and Prometheus' Gauge API, switching between them based on the service type.
    """

    class NumericJobStatus(Enum):
        RECEIVED = 1
        APPROVED = 2
        RUNNING = 3
        COMPLETED = 4
        LOG = 5
        ERROR = 6
        STREAM = 7
        NNSIGHT_ERROR = 8

    name: str = "request_status"

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
        numeric_status = int(cls.NumericJobStatus[response.status.value].value)

        super().update(
            numeric_status,
            request_id=str(request.id),
            api_key=str(request.api_key),
            model_key=str(request.model_key),
            user_id=" ",
            msg=response.description,
        )
