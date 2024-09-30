import logging
import logging_loki
import os

# URL for Loki logging service, retrieved from environment variables
LOKI_URL = os.environ.get('LOKI_URL')

class CustomJSONFormatter(logging.Formatter):
    """
    A custom JSON formatter for logging that includes the service name in the log record.
    """

    def __init__(self, service_name, fmt=None, datefmt=None, style='%', *args, **kwargs):
        """
        Initialize the CustomJSONFormatter.

        Args:
            service_name (str): The name of the service to be included in logs (e.g. 'app' or 'ray')
            fmt (str, optional): The log format string. Defaults to None.
            datefmt (str, optional): The date format string. Defaults to None.
            style (str, optional): The style of the fmt string. Defaults to '%'.
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.
        """
        super().__init__(fmt=fmt, datefmt=datefmt, style=style, *args, **kwargs)
        self.service_name = service_name

    def format(self, record):
        """
        Format the specified record as text.

        Args:
            record (LogRecord): The log record to be formatted.

        Returns:
            str: The formatted log record.
        """
        # Add the service_name to the log record
        record.service_name = self.service_name
        
        # Format the log record using the standard logging format
        return super().format(record)

def load_logger(service_name: str, logger_name: str) -> logging.Logger:
    """
    Configure and return a logger with the specified service name and logger name. 
    If the Loki URL is not set, the logger will only log to the console.
    Args:
        service_name (str): The name of the service.
        logger_name (str): The name of the logger.

    Returns:
        logging.Logger: A configured logger instance.
    """

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Define the log format as a JSON string
    log_format = '{"datetime": "%(asctime)s", "service_name": "%(service_name)s", "level": "%(levelname)s", "function": "%(funcName)s", "message": "%(message)s"}'
    formatter = CustomJSONFormatter(fmt=log_format, service_name=service_name)

    if LOKI_URL is not None:
        # Loki handler configuration
        loki_handler = logging_loki.LokiHandler(
            url=LOKI_URL,
            tags={"application": service_name},
            auth=None,
            version="1",
        )
        loki_handler.setFormatter(formatter)
        logger.addHandler(loki_handler)

    # Add a console handler for local logging
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger