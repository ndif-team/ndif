import time
from typing import TYPE_CHECKING

from influxdb_client import Point

from . import Metric

if TYPE_CHECKING:

    from ..schema import BackendRequestModel, BackendResponseModel


class StageLatencyGauge(Metric):

    measurement: str = "stage_latency"
    field: str = "latency"

    @classmethod
    def update(
        cls,
        request: "BackendRequestModel",
        status: "BackendResponseModel.JobStatus",
    ):

        now = time.time()

        if request.last_status_update is not None and status.name != "ERROR":

            delta = now - request.last_status_update

            point: Point = Point(cls.measurement).tag("request_id", request.id).field(cls.field, delta)

            super().update(point)

        request.last_status_update = now
