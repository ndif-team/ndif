from typing import TYPE_CHECKING, Any
from . import Metric
from fastapi import Request
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
        raw_request: Request,

    ) -> None:

        super().update(
            int(raw_request.headers.get("content-length", 0)),
            request_id=request.id,
            model_key=request.model_key,
            api_key=request.api_key,
            ip_address=raw_request.client.host,
            user_agent=raw_request.headers.get("user-agent")        )
