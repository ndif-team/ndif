from typing import TYPE_CHECKING

from . import Metric

if TYPE_CHECKING:
    from ..schema.request import BackendRequestModel


class QueueWaitTimeMetric(Metric):
    name: str = "queue_wait_time"

    @classmethod
    def update(cls, request: "BackendRequestModel", wait_time_s: float) -> None:
        super().update(
            wait_time_s,
            request_id=str(request.id),
            api_key=str(request.api_key),
            model_key=str(request.model_key),
        )
