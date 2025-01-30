import time
from typing import TYPE_CHECKING

from . import Metric

if TYPE_CHECKING:

    from ..schema import BackendRequestModel, BackendResponseModel


class StageLatencyGauge(Metric):

    name = "stage_latency"
    description = "Latency in seconds between stages"
    tags = ("request_id", "stage")

    @classmethod
    def update(
        cls,
        request: "BackendRequestModel",
        status: "BackendResponseModel.JobStatus",
        **kwargs
    ):

        now = time.time()

        if request.last_status_update is not None and status.name != "ERROR":

            delta = now - request.last_status_update

            super().update(
                delta,
                request_id=request.id,
                stage=status.name,
                **kwargs,
                ray=status.name != "RECEIVED"
            )

        request.last_status_update = now
