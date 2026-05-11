from typing import TYPE_CHECKING

from . import Metric

if TYPE_CHECKING:
    from ..schema.request import BackendRequestModel


class RequestErrorMetric(Metric):
    name: str = "request_error"

    @classmethod
    def update(cls, request: "BackendRequestModel") -> None:
        super().update(
            1,
            request_id=str(request.id),
            api_key=str(request.api_key),
            model_key=str(request.model_key),
        )
