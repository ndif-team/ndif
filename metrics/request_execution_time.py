from typing import TYPE_CHECKING
from . import Metric

if TYPE_CHECKING:

    from ..schema import BackendRequestModel


class ExecutionTimeMetric(Metric):
    
    name:str = "request_execution_time"
    
    @classmethod
    def update(cls, request: "BackendRequestModel", time_s:float):    
            
        super().update(
            time_s, 
            request_id=request.id,
            api_key=request.api_key,
            model_key=request.model_key,
        )