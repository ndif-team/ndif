from typing import TYPE_CHECKING
from . import Metric

if TYPE_CHECKING:

    from ..schema import BackendRequestModel


class TransportLatencyMetric(Metric):
    
    name:str = "request_transport_latency"
    
    @classmethod
    def update(cls, request: "BackendRequestModel"):    
        
        if request.sent is not None:
    
            super().update(request.received - request.sent, request_id=request.id)