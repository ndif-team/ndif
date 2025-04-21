from typing import TYPE_CHECKING
from ndif.core.telemetry.metrics import Metric

if TYPE_CHECKING:

    from ndif.core.schema import BackendRequestModel


class GPUMemMetric(Metric):
    
    name:str = "request_gpu_mem"
    
    @classmethod
    def update(cls, request: "BackendRequestModel", gpu_mem:float):    
    
        super().update(gpu_mem, request_id=request.id)