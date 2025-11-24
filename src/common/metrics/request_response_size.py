from typing import TYPE_CHECKING
from . import Metric

if TYPE_CHECKING:
    from ..schema import BackendRequestModel


class RequestResponseSizeMetric(Metric):
    name: str = "request_response_size"

    @classmethod
    def update(cls, request: "BackendRequestModel", size: int):
        super().update(
            size,
            request_id=request.id,
            api_key=request.api_key,
            model_key=request.model_key,
        )
