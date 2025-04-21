from typing import TYPE_CHECKING
from ndif.core.telemetry.metrics import Metric

if TYPE_CHECKING:

    from ndif.core.schema import BackendRequestModel


class ExecutionTimeMetric(Metric):
    
    name:str = "request_execution_time"
    
    @classmethod
    def update(cls, request: "BackendRequestModel", time_s:float):    
            
        super().update(time_s, request_id=request.id)