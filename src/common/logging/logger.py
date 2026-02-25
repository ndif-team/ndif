import logging

import os
import socket
import sys
import traceback
from typing import Dict, Optional, Any
import time
from functools import wraps

from opentelemetry import trace
from opentelemetry.trace import format_trace_id, format_span_id

# Environment variables for Loki configuration
LOKI_URL = os.environ.get("LOKI_URL")
LOKI_RETRY_COUNT = int(
    os.environ.get("LOKI_RETRY_COUNT", "3")
)  # Number of retry attempts for failed log sends


if LOKI_URL is not None:
    import logging_loki


class CustomJSONFormatter(logging.Formatter):
    """
    Custom JSON formatter for structured logging.

    Extends the standard logging.Formatter to add additional fields
    like service name, hostname, and metrics data to log records.
    """

    def __init__(
        self, service_name, fmt=None, datefmt=None, style="%", *args, **kwargs
    ):
        """
        Initialize the formatter with service information.

        Args:
            service_name: Name of the service generating logs
            fmt: Log format string
            datefmt: Date format string
            style: Style of the format string (%, {, or $)
        """
        super().__init__(fmt=fmt, datefmt=datefmt, style=style, *args, **kwargs)
        self.service_name = service_name
        self.hostname = socket.gethostname()

    def format(self, record):
        """
        Format the log record by adding custom fields.

        Args:
            record: The log record to format

        Returns:
            Formatted log record as a string
        """
        # Add custom fields to the log record
        record.service_name = self.service_name
        record.hostname = self.hostname
        record.process_id = os.getpid()
        record.thread_name = record.threadName
        # Add code location
        record.code_file = record.pathname
        record.code_line = record.lineno

        # Add trace context if available
        span = trace.get_current_span()
        span_context = span.get_span_context()
        if span_context and span_context.is_valid:
            record.trace_id = format_trace_id(span_context.trace_id)
            record.span_id = format_span_id(span_context.span_id)
        else:
            record.trace_id = ""
            record.span_id = ""

        # Format the log record using the standard logging format
        return super().format(record)


if LOKI_URL is not None:

    class RetryingLokiHandler(logging_loki.LokiHandler):
        """
        Extended Loki handler with retry capability for handling network issues.

        Attempts to resend logs to Loki if initial attempts fail, using
        exponential backoff between retries.
        """

        def __init__(self, retry_count=LOKI_RETRY_COUNT, *args, **kwargs):
            """
            Initialize the handler with retry configuration.

            Args:
                retry_count: Number of times to retry sending logs
                *args, **kwargs: Arguments passed to LokiHandler
            """
            self.retry_count = retry_count
            super().__init__(*args, **kwargs)

        def emit(self, record):
            """
            Send the log record to Loki with retry logic.

            Args:
                record: The log record to send
            """
            for attempt in range(self.retry_count):
                try:
                    super().emit(record)
                    return
                except Exception as e:
                    if attempt == self.retry_count - 1:
                        sys.stderr.write(
                            f"Failed to send log to Loki after {self.retry_count} attempts: {e}\n"
                        )
                    else:
                        time.sleep(0.5 * (attempt + 1))  # Exponential backoff


def set_logger(service_name) -> logging.Logger:
    logger = logging.getLogger("ndif")

    if service_name is None:
        raise ValueError("Service name is required")

    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # JSON format for structured logging (used by Loki handler)
    json_format = """{
        "timestamp": "%(asctime)s",
        "service": {
            "name": "%(service_name)s",
            "hostname": "%(hostname)s",
            "process_id": %(process_id)d
        },
        "log": {
            "level": "%(levelname)s",
            "logger": "%(name)s",
            "function": "%(funcName)s",
            "thread": "%(thread_name)s"
        },
        "code": {
            "file": "%(code_file)s",
            "line": %(code_line)d
        },
        "trace": {
            "trace_id": "%(trace_id)s",
            "span_id": "%(span_id)s"
        },
        "message": "%(message)s"
    }"""

    # Simpler format for console output with filename and process id
    console_format = "[%(asctime)s] [%(process)d] [%(levelname)s] [trace:%(trace_id)s] [%(pathname)s:%(lineno)d] %(message)s"
    # Create formatters for different outputs
    json_formatter = CustomJSONFormatter(
        fmt=json_format, service_name=service_name, datefmt="%Y-%m-%d %H:%M:%S.%f%z"
    )

    console_formatter = CustomJSONFormatter(
        fmt=console_format, service_name=service_name, datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Set up console handler for local debugging
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    # Set up Loki handler if URL is configured
    if LOKI_URL is not None:
        # Loki handler configuration with batching and retries
        loki_handler = RetryingLokiHandler(
            url=LOKI_URL,
            tags={
                "application": service_name,
                "hostname": socket.gethostname(),
            },
            auth=None,
            version="1",
        )
        loki_handler.setFormatter(json_formatter)
        loki_handler.setLevel(logging.INFO)  # Only send INFO and above to Loki
        logger.addHandler(loki_handler)

    return logger
