from enum import Enum
from prometheus_client import Gauge as PrometheusGauge
from typing import Optional
from nnsight.schema.Request import RequestModel
from nnsight.schema.Response import ResponseModel

# Labels for the metrics
request_labels = ('request_id', 'api_key', 'model_key', 'timestamp')

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

    class NumericJobStatus(Enum):
        RECEIVED = 1
        APPROVED = 2
        RUNNING = 3
        COMPLETED = 4
        LOG = 5
        ERROR = 6

    def __init__(self, service: str):
        self.service = service
        self._gauge = self._initialize_gauge()

    def _initialize_gauge(self):
        """Initialize the appropriate Gauge based on the service type."""
        if self.service == 'ray':
            from ray.util.metrics import Gauge as RayGauge
            return RayGauge('request_status', description='Track status of requests', tag_keys=request_labels)
        else:
            return PrometheusGauge('request_status', 'Track status of requests', request_labels)

    def update(self, request: RequestModel, api_key: str, status) -> None:
        """
        Update the values of the gauge to reflect the current status of a request.
        Handles both Ray and Prometheus Gauge APIs.
        """
        numeric_status = int(self.NumericJobStatus[status.value].value)
        labels = {
            "request_id": request.id,
            "api_key": api_key,
            "model_key": request.model_key,
            "timestamp": str(request.received)  # Ensure timestamp is string for consistency
        }

        if self.service == 'ray':
            # Ray's API uses a different method for setting gauge values
            self._gauge.set(numeric_status, tags=labels)
        else:
            # Prometheus Gauge API uses a more traditional labeling approach
            self._gauge.labels(**labels).set(numeric_status)