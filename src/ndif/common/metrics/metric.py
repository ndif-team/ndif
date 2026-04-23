import atexit
import os
from typing import TYPE_CHECKING, Any, Optional, Union


import logging

if os.getenv("INFLUXDB_ADDRESS") is not None:
    from influxdb_client import InfluxDBClient, WriteApi
    from influxdb_client.client.write_api import WriteOptions
    from influxdb_client import Point
else:
    WriteApi = Any
    Point = Any
    InfluxDBClient = Any

logger = logging.getLogger("ndif")


class Metric:
    name: str
    _influx_client: Optional[Any] = None
    client: Optional[WriteApi] = None

    @classmethod
    def _init_client(cls):
        if cls._influx_client is not None:
            return
        cls._influx_client = InfluxDBClient(
            url=os.getenv("INFLUXDB_ADDRESS"),
            token=os.getenv("INFLUXDB_ADMIN_TOKEN"),
        )
        cls.client = cls._influx_client.write_api(
            write_options=WriteOptions(
                batch_size=100,
                flush_interval=5_000,
            )
        )
        atexit.register(cls._shutdown)

    @classmethod
    def _shutdown(cls):
        if cls.client is not None:
            try:
                cls.client.close()
            except Exception:
                pass
        if cls._influx_client is not None:
            try:
                cls._influx_client.close()
            except Exception:
                pass

    @classmethod
    def update(cls, measurement: Union[Any, Point], **tags):
        try:
            if cls.client is None and os.getenv("INFLUXDB_ADDRESS") is not None:
                cls._init_client()

            if cls.client is None:
                return

            if isinstance(measurement, Point):
                point = measurement
            else:
                point: Point = Point(cls.name).field(cls.name, measurement)

                for key, value in tags.items():
                    point = point.tag(key, value)

            cls.client.write(
                bucket=os.getenv("INFLUXDB_BUCKET"),
                org=os.getenv("INFLUXDB_ORG"),
                record=point,
            )

        except Exception as e:
            logger.exception(f"Error updating metric {cls.name}: {e}")
