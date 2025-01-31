from typing import Dict, Tuple, Union
from ray.util.metrics import Gauge as RayGauge, Counter as RayCounter
from prometheus_client import Gauge as PrometheusGauge, Counter as PrometheusCounter


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

class CounterMetric:
    name = ""
    description = ""

    def __init_subclass__(cls):
                
        cls.counter: Union[RayCounter, PrometheusCounter] = None

    @classmethod
    def update(cls, value: float = 1, ray: bool = True):

        if cls.counter is None:
            counter_cls = RayCounter if ray else PrometheusCounter
            cls.counter = counter_cls(cls.name, cls.description)
            
        cls.counter.inc(value)

