from typing import TYPE_CHECKING
from . import Metric

if TYPE_CHECKING:

    from ..schema import BackendRequestModel


class GPUMemGauge(Metric):
    
    name = "gpu_mem"
    description = "GPU Mem of requests"
    tags = ("request_id",)

    @classmethod
    def update(cls, request: "BackendRequestModel", gpu_mem:float, **kwargs):    
    
        super().update(gpu_mem, request_id=request.id, **kwargs)