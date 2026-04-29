from typing import TYPE_CHECKING, Any
from . import Metric

if TYPE_CHECKING:
    from ..schema import BackendRequestModel
else:
    BackendRequestModel = Any


class NetworkStatusMetric(Metric):
    name: str = "network_data"

    @classmethod
    def update(
        cls,
        request: BackendRequestModel,
    ) -> None:
        super().update(
            request.content_length,
            request_id=request.id,
            model_key=request.model_key,
            api_key=request.api_key,
            ip_address=request.ip_address,
            user_agent=request.user_agent,
        )
