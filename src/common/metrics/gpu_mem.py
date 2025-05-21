from typing import TYPE_CHECKING
from . import Metric

if TYPE_CHECKING:

    from ..schema import BackendRequestModel


class GPUMemMetric(Metric):
    
    name:str = "request_gpu_mem"
    
    @classmethod
    def update(cls, request: "BackendRequestModel", gpu_mem:float):    
    
        super().update(
            gpu_mem, 
            request_id=request.id,
            api_key=request.api_key,
            model_key=request.model_key,
        )