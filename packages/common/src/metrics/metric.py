import os
from typing import TYPE_CHECKING, Any, Optional, Union

from influxdb_client import InfluxDBClient, WriteApi
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client import Point

import logging
logger = logging.getLogger("ndif")


class Metric:

    name: str
    client: Optional[WriteApi] = None

    @classmethod
    def update(cls, measurement: Union[Any, Point], **tags):
        
        try:

            if Metric.client is None:

                Metric.client = InfluxDBClient(
                    url=os.getenv("INFLUXDB_ADDRESS"),
                    token=os.getenv("INFLUXDB_ADMIN_TOKEN"),
                ).write_api(write_options=SYNCHRONOUS)

            # If youre providing a Point directly, use it as is
            if isinstance(measurement, Point):
                point = measurement
                
            #Otherwise build it from the value (measurement) and its tags.
            else:

                point: Point = Point(cls.name).field(cls.name, measurement)

                for key, value in tags.items():

                    point = point.tag(key, value)

            Metric.client.write(
                bucket=os.getenv("INFLUXDB_BUCKET"),
                org=os.getenv("INFLUXDB_ORG"),
                record=point,
            )

        except Exception as e:
            logger.exception(f"Error updating metric {cls.name}: {e}")
