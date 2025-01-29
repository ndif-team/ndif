from typing import Dict, Tuple, Union
from ray.util.metrics import Gauge as RayGauge
from prometheus_client import Gauge as PrometheusGauge


class Metric:

    name = ""
    tags = tuple()
    description = ""

    def __init_subclass__(cls):
                
        cls.gauge: Union[RayGauge, PrometheusGauge] = None

    @classmethod
    def update(cls, value:float, ray: bool = True, **tags):

        if cls.gauge is None:

            gauge_cls = RayGauge if ray else PrometheusGauge

            cls.gauge = gauge_cls(cls.name, cls.description, cls.tags)
            
        if ray:
            cls.gauge.set(value, tags=tags)
        else:
            cls.gauge.labels(**tags).set(value)


