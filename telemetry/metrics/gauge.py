from prometheus_client import Gauge

def load_gauge() -> Gauge:
    '''Simple helper function which initializes a Gauge, which is used for transmitting metrics to a Prometheus backend.'''
    request_labels = ['request_id', 'api_key', 'model_key', 'timestamp']
    request_status = Gauge('request_status', 'Track status of requests', request_labels)
    return request_status