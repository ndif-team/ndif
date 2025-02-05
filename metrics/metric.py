import os
from typing import TYPE_CHECKING

from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS

if TYPE_CHECKING:
    from influxdb_client import Point

class Metric:

    def __init_subclass__(cls):
        cls.gauge: InfluxDBClient = None
    
    @classmethod
    def update(cls, point: "Point"):

        if cls.gauge is None:

            cls.gauge = InfluxDBClient(url=os.getenv("INFLUXDB_ADDRESS"), token=os.getenv("INFLUXDB_ADMIN_TOKEN"), org=os.getenv("INFLUXDB_ORG")).write_api(write_options=SYNCHRONOUS)

        cls.gauge.write(bucket=os.getenv("INFLUXDB_BUCKET"), org=os.getenv("INFLUXDB_ORG"), record=point)


