import os
from typing import Any

if os.getenv("INFLUXDB_ADDRESS") is not None:
    from influxdb_client import Point
else:
    Point = Any

from . import Metric


class QueueDepthMetric(Metric):
    name: str = "queue_depth"

    @classmethod
    def update(cls, model_key: str, depth: int, dispatched: int) -> None:
        point = (
            Point(cls.name)
            .tag("model_key", model_key)
            .field("depth", depth)
            .field("dispatched", dispatched)
        )
        super().update(point)
