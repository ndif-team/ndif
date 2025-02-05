from influxdb_client import Point

from . import Metric

class NetworkStatusGauge(Metric):

    measurement: str = "network_data"
    field: str = "content_lenght"

    @classmethod
    def update(
        cls,
        request_id: str,
        ip_address: str,
        user_agent: str,
        content_length: int,
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

        point: Point = Point(cls.measurement)

        for tag, value in network_labels.items():
            point.tag(tag, value)

        point.field(cls.field, content_length)

        super().update(point)
