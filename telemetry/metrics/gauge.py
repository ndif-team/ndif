from prometheus_client import Gauge
from typing import Optional

from nnsight.schema.Request import RequestModel
from nnsight.schema.Response import ResponseModel

# Global variable to store the Gauge instance
_gauge: Optional[Gauge] = None

def load_gauge() -> Gauge:
    '''Simple helper function which initializes a Gauge, which is used for transmitting metrics to a Prometheus backend.
    Uses a singleton pattern to ensure only one instance is created. 
    '''
    global _gauge
    if _gauge is None:
        request_labels = ['request_id', 'api_key', 'model_key', 'timestamp']
        _gauge = Gauge('request_status', 'Track status of requests', request_labels)
    return _gauge

def update_gauge(request: RequestModel, api_key: str, status: ResponseModel.JobStatus) -> None:
    '''Helper function used to update the values of a gauge. This enables desired metrics to be updated in real-time.'''
    gauge = load_gauge()
    gauge.labels(
        request_id=request.id,
        api_key=api_key,
        model_key=request.model_key,
        timestamp=request.received,
    ).set(status.value)
