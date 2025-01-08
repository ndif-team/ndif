from enum import Enum
from typing import Optional

from prometheus_client import Gauge as PrometheusGauge

from nnsight.schema.request import RequestModel
from nnsight.schema.response import ResponseModel

# Labels for the metrics
request_labels = (
    "request_id",
    "api_key",
    "model_key",
    "gpu_mem",
    "timestamp",
    "user_id",
    "msg",
)
network_labels = ("request_id", "ip_address", "user_agent")


class NDIFGauge:
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

    # Class-level dictionary to store gauge instances (singleton pattern)
    _instances = {}

    class NumericJobStatus(Enum):
        RECEIVED = 1
        APPROVED = 2
        RUNNING = 3
        COMPLETED = 4
        LOG = 5
        ERROR = 6
        STREAM = 7
        NNSIGHT_ERROR = 8

    def __new__(cls, service: str):
        """Singleton pattern to ensure only one instance of the gauge per service."""
        if service not in cls._instances:
            instance = super(NDIFGauge, cls).__new__(cls)
            instance.service = service
            instance._gauge = instance._initialize_gauge()
            if (
                service != "ray"
            ):  # Only initialize the network gauge if the service is not 'ray'
                instance._network_gauge = instance._initialize_network_gauge()
            cls._instances[service] = instance
        return cls._instances[service]

    def _initialize_gauge(self):
        """Initialize the appropriate Gauge based on the service type."""
        if self.service == "ray":
            from ray.util.metrics import Gauge as RayGauge

            return RayGauge(
                "request_status",
                description="Track status of requests",
                tag_keys=request_labels,
            )
        else:
            return PrometheusGauge(
                "request_status", "Track status of requests", request_labels
            )

    def _initialize_network_gauge(self):
        """Initialize the network-related Gauge. Only used if the service is not 'ray'."""
        return PrometheusGauge(
            "network_data", "Track network data of requests", network_labels
        )

    def update(
        self,
        request: RequestModel,
        status: ResponseModel.JobStatus,
        api_key: str = "",
        user_id=None,
        gpu_mem: int = 0,
        msg: str = "",
    ) -> None:
        """
        Update the values of the gauge to reflect the current status of a request.
        Handles both Ray and Prometheus Gauge APIs.
        """
        numeric_status = int(self.NumericJobStatus[status.value].value)

        labels = {
            "request_id": str(request.id),
            "api_key": str(api_key),
            "model_key": str(request.model_key),
            "gpu_mem": str(gpu_mem),
            "timestamp": str(
                request.received
            ),  # Ensure timestamp is string for consistency
            "user_id": str(user_id) if user_id is not None else " ",
            "msg": msg,
        }

        if self.service == "ray":
            # Ray's API uses a different method for setting gauge values
            self._gauge.set(numeric_status, tags=labels)
        else:
            # Prometheus Gauge API uses a more traditional labeling approach
            self._gauge.labels(**labels).set(numeric_status)

    def update_network(
        self,
        request_id: str,
        ip_address: str,
        user_agent: str,
        content_length: int,
    ) -> None:
        """
        Update the values of the network-related gauge.
        Only applicable for services other than 'ray'.
        """
        if self.service == "ray":
            return  # Do nothing if the service is 'ray'

        network_labels = {
            "request_id": request_id,
            "ip_address": ip_address,
            "user_agent": user_agent,
        }

        # Set content length in the network gauge
        self._network_gauge.labels(**network_labels).set(content_length)
