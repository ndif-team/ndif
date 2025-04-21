import time
from typing import TYPE_CHECKING
from ndif.core.telemetry.metrics import Metric

if TYPE_CHECKING:

    from ndif.core.schema import BackendRequestModel


class TransportLatencyMetric(Metric):
    
    name:str = "request_transport_latency"
    
    @classmethod
    def update(cls, request: "BackendRequestModel"):    
        
        if request.sent is not None:
    
            super().update(time.time() - request.sent, request_id=request.id)