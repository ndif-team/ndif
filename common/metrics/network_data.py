
from . import Metric


class NetworkStatusMetric(Metric):

    name: str = "network_data"

    @classmethod
    def update(
        cls,
        request_id: str,
        ip_address: str,
        user_agent: str,
        content_length: int,
    ) -> None:

        super().update(
            content_length,
            request_id=request_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
