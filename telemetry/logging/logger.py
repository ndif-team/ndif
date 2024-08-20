import json
import logging
import logging_loki
from datetime import datetime

class CustomJSONFormatter(logging.Formatter):
    def __init__(self, service_name, fmt=None, datefmt=None, style='%', *args, **kwargs):
        super().__init__(fmt=fmt, datefmt=datefmt, style=style, *args, **kwargs)
        self.service_name = service_name

    def format(self, record):
        # Add the service_name to the log record
        record.service_name = self.service_name
        
        # Format the log record using the standard logging format
        return super().format(record)

def load_logger(service_name : str, logger_name : str) -> logging.Logger:

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    log_format = '{"datetime": "%(asctime)s", "service_name": "%(service_name)s", "level": "%(levelname)s", "function": "%(funcName)s", "message": "%(message)s"}'
    formatter = CustomJSONFormatter(fmt=log_format, service_name=service_name)

    # Loki handler configuration
    loki_handler = logging_loki.LokiHandler(
        url="http://localhost:3100/loki/api/v1/push", # Loki endpoint
        tags={"application": service_name},
        auth=None,
        version="1",
    )
    loki_handler.setFormatter(formatter)

    logger.addHandler(loki_handler)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger