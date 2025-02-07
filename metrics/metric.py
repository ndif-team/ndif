import os
from typing import TYPE_CHECKING, Any, Optional, Union

from influxdb_client import InfluxDBClient, WriteApi
from influxdb_client.client.write_api import SYNCHRONOUS
from influxdb_client import Point


class Metric:

    name: str
    client: Optional[WriteApi] = None

    @classmethod
    def update(cls, measurement: Union[Any,Point], **tags):

        if Metric.client is None:

            Metric.client = InfluxDBClient(
                url=os.getenv("INFLUXDB_ADDRESS"),
                token=os.getenv("INFLUXDB_ADMIN_TOKEN"),
            ).write_api(write_options=SYNCHRONOUS)
            
        if isinstance(measurement, Point):
            point = measurement
        else:

            point: Point = Point(cls.name).field(cls.name, measurement)

            for key, value in tags.items():

                point = point.tag(key, value)

        Metric.client.write(bucket=os.getenv("INFLUXDB_BUCKET"), org=os.getenv("INFLUXDB_ORG"), record=point)
