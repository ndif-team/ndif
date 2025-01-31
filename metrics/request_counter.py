from . import CounterMetric

class RequestCounter(CounterMetric):
    name = "request_counter"
    description = "Tracks number of requests with time"
