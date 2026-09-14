"""Shared logging setup: readable console output + record field extraction.

Two consumers share this module so the console and the Loki line agree on what
a record *means*:

  * :func:`structured_fields` pulls the ``extra={...}`` fields off a record
    (everything that isn't a stdlib ``LogRecord`` attribute).
  * :func:`source_context` / :func:`exception_fields` derive the host, code
    location, and (split) exception info both destinations attach.

:class:`ConsoleFormatter` renders all of that as one readable line
(``ts LEVEL [logger] message key=value … (file:line)``) with the traceback on
following lines. :func:`configure_console` installs it on the ``ndif`` logger
(idempotently) and stops propagation so records aren't *also* printed by a root
handler (e.g. ``logging.basicConfig`` in the dispatcher, or gunicorn's).
"""

import logging
import os
import socket
import sys
from typing import Any, Dict

# Resolved once per process; attached to every shipped record so multi-node
# deployments can tell which machine emitted a log.
HOSTNAME = socket.gethostname()

# LogRecord attributes present on a bare record — anything else on a record's
# __dict__ arrived via ``extra=`` and is treated as a structured field.
# ``tags`` is excluded too: the Loki provider injects it as a control attribute
# for per-record stream labels, not as a data field for the line.
_RESERVED_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
    "tags",
}


def structured_fields(record: logging.LogRecord) -> Dict[str, Any]:
    """The ``extra={...}`` fields attached to ``record`` (order preserved)."""
    return {
        k: v
        for k, v in record.__dict__.items()
        if k not in _RESERVED_RECORD_KEYS and not k.startswith("_")
    }


def source_context(record: logging.LogRecord) -> Dict[str, Any]:
    """Host + code-location context: where (and by whom) the log was emitted."""
    return {
        "host": HOSTNAME,
        "pid": record.process,
        "thread": record.threadName,
        "func": record.funcName,
        "file": record.filename,
        "line": record.lineno,
    }


def exception_fields(record: logging.LogRecord) -> Dict[str, Any]:
    """Split exception info: type + message (queryable) plus the full stack."""
    if not record.exc_info or record.exc_info[0] is None:
        return {}
    exc_type, exc_value, _ = record.exc_info
    fields: Dict[str, Any] = {
        "exception_type": exc_type.__name__,
        "stacktrace": logging.Formatter().formatException(record.exc_info),
    }
    if exc_value is not None:
        fields["exception_message"] = str(exc_value)
    return fields


class ConsoleFormatter(logging.Formatter):
    """Human-readable console line that also surfaces the structured fields.

    Example::

        2026-07-06 12:00:01 INFO    [ndif.queue.replica] request completed
        model_key=gpt2 request_id=ab12 exec_ms=1200.5 (replica.py:281)

    The traceback (if any) follows on subsequent lines, as usual.
    """

    #: extra values longer than this are truncated in the console (the full
    #: value still ships to Loki).
    _MAX_VALUE_LEN = 200

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        head = f"{ts} {record.levelname:<7} [{record.name}] {record.getMessage()}"

        fields = structured_fields(record)
        if fields:
            head += " " + " ".join(
                f"{k}={self._render(v)}" for k, v in fields.items()
            )

        head += f" ({record.filename}:{record.lineno})"

        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            head += "\n" + record.exc_text
        if record.stack_info:
            head += "\n" + self.formatStack(record.stack_info)
        return head

    def _render(self, value: Any) -> str:
        text = str(value)
        if len(text) > self._MAX_VALUE_LEN:
            text = text[: self._MAX_VALUE_LEN] + "…"
        return text


def configure_console(name: str = "ndif") -> None:
    """Install the readable console handler on the ``name`` logger. Idempotent.

    Also sets ``propagate = False`` so records aren't duplicated by a root
    handler, and sets the logger level from ``NDIF_LOG_LEVEL`` (default INFO)
    unless it is already lower (e.g. the Loki handler wants DEBUG).
    """
    logger = logging.getLogger(name)
    logger.propagate = False

    level = os.environ.get("NDIF_LOG_LEVEL", "INFO").upper()
    level_no = logging.getLevelName(level)
    if not isinstance(level_no, int):
        level_no = logging.INFO
    if logger.level == logging.NOTSET or logger.level > level_no:
        logger.setLevel(level_no)

    if any(getattr(h, "_ndif_console", False) for h in logger.handlers):
        return

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(ConsoleFormatter())
    handler.setLevel(level_no)
    handler._ndif_console = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
