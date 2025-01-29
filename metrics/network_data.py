from . import Metric


class NetworkStatusGauge(Metric):

    name = "network_data"
    description = "Track network data of requests"
    tags = ("request_id", "ip_address", "user_agent")

    @classmethod
    def update(
        cls,
        request_id: str,
        ip_address: str,
        user_agent: str,
        content_length: int,
        **kwargs
    ) -> None:
        """
        Update the values of the network-related gauge.
        Only applicable for services other than 'ray'.
        """

        network_labels = {
            "request_id": request_id,
            "ip_address": ip_address,
            "user_agent": user_agent,
        }

        super().update(content_length, **network_labels, **kwargs, ray=False)
