import logging
import os
import socket
import sys
import traceback

from opentelemetry import trace
from opentelemetry.trace import format_trace_id, format_span_id

LOKI_URL = os.environ.get("LOKI_URL")

if LOKI_URL is not None:
    from loki_logger_handler.loki_logger_handler import LokiLoggerHandler


class LokiFormatter:
    """Formatter for LokiLoggerHandler that returns (dict, dict).

    Produces a flat JSON structure optimized for Grafana LogQL queries.
    All fields are top-level for easy filtering: {message="...", level="ERROR", ...}
    """

    LOG_RECORD_BUILTINS = {
        "msg", "args", "exc_info", "exc_text", "stack_info",
        "levelname", "levelno", "name", "pathname", "filename",
        "module", "lineno", "funcName", "created", "msecs",
        "relativeCreated", "thread", "threadName", "process",
        "processName", "message", "taskName",
    }

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.hostname = socket.gethostname()

    def format(self, record: logging.LogRecord) -> tuple[dict, dict]:
        entry = {
            "message": record.getMessage(),
            "timestamp": record.created,
            "level": record.levelname,
            "logger": record.name,
            "service": self.service_name,
            "hostname": self.hostname,
            "pid": record.process,
            "thread": record.threadName,
            "func": record.funcName,
            "file": record.pathname,
            "line": record.lineno,
        }

        # Trace context
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            entry["trace_id"] = format_trace_id(ctx.trace_id)
            entry["span_id"] = format_span_id(ctx.span_id)

        # Exception info
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception_type"] = record.exc_info[0].__name__
            entry["exception_message"] = str(record.exc_info[1])
            entry["stacktrace"] = "".join(
                traceback.format_exception(*record.exc_info)
            )

        # Capture any extra fields passed via logger.info("msg", extra={...})
        for key, value in record.__dict__.items():
            if key not in self.LOG_RECORD_BUILTINS and key not in entry:
                entry[key] = value

        return entry, {}


class ConsoleFormatter(logging.Formatter):
    """Formatter for console/stdout with trace context."""

    def __init__(self, service_name: str):
        super().__init__(
            fmt="[%(asctime)s] [%(process)d] [%(levelname)s] [%(pathname)s:%(lineno)d] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            record.trace_id = format_trace_id(ctx.trace_id)
        else:
            record.trace_id = ""

        return super().format(record)


def set_logger(service_name: str) -> logging.Logger:
    if service_name is None:
        raise ValueError("Service name is required")

    logger = logging.getLogger("ndif")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # Console handler for local debugging
    console_handler = logging.StreamHandler(stream=sys.stdout)
    console_handler.setFormatter(ConsoleFormatter(service_name))
    console_handler.setLevel(logging.DEBUG)
    logger.addHandler(console_handler)

    # Loki handler for production log aggregation
    if LOKI_URL is not None:
        loki_handler = LokiLoggerHandler(
            url=LOKI_URL,
            labels={
                "application": service_name,
                "hostname": socket.gethostname(),
            },
            label_keys={"level"},
            timeout=10,
            compressed=True,
            default_formatter=LokiFormatter(service_name),
        )
        loki_handler.setLevel(logging.INFO)
        logger.addHandler(loki_handler)

    return logger
