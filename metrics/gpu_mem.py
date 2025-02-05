from typing import TYPE_CHECKING
from . import Metric

from influxdb_client import Point

if TYPE_CHECKING:

    from ..schema import BackendRequestModel


class GPUMemGauge(Metric):
    
    measurement: str = "gpu_mem"
    field: str = "gpu_mem"

    @classmethod
    def update(cls, request: "BackendRequestModel", gpu_mem:float):    

        point: Point = Point(cls.measurement).tag("request_id", request.id).field(cls.field, gpu_mem)
    
        super().update(point)