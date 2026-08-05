"""Structured telemetry helpers over stdlib logging.

The :class:`~ndif.common.providers.loki.LokiProvider` handler already ships
every ``ndif`` log record to Loki; this module is the thin, consistent way to
attach *structured fields* to those records so they're queryable in Grafana
(``| json | duration_ms > 1000`` and the like) instead of buried in free text.

Use :func:`event` instead of ``logger.info(msg, extra={...})`` directly: it
drops ``None`` fields (so optional ids don't clutter the line) and refuses keys
that would collide with reserved ``LogRecord`` attributes (which otherwise makes
``logging`` raise). Field names are conventions, not a schema — keep them
stable so dashboards keep working. Common ones:

    model_key, replica_id            # who served it (model_key is also a label)
    request_id, session_id           # correlate one job end-to-end
    stage, status                    # lifecycle position
    duration_ms, wait_ms             # latency signals
    queue_size, queue_position       # depth / throughput signals
    replicas_before, replicas_after  # autoscaling signals
    error_type                       # exception class, for error dashboards
"""

import logging
import time
from typing import Any, Dict

# LogRecord attributes that must never be overwritten by an ``extra`` key —
# ``logging`` raises KeyError if an extra collides with one of these.
_RESERVED = set(logging.makeLogRecord({}).__dict__.keys()) | {
    "message",
    "asctime",
}


def _clean(fields: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in fields.items():
        if value is None or key in _RESERVED:
            continue
        out[key] = value
    return out


def event(
    logger: logging.Logger,
    message: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: Any,
) -> None:
    """Emit a structured log record: ``message`` plus keyword ``fields``.

    ``None`` fields and reserved-name fields are dropped. ``exc_info=True``
    attaches the current exception (split into ``exception_type`` /
    ``exception_message`` / ``stacktrace``).

    ``stacklevel=2`` attributes the record to *this function's caller* rather
    than to ``event`` itself, so the ``func`` / ``file`` / ``line`` source
    context points at the real call site.
    """
    logger.log(
        level, message, extra=_clean(fields), exc_info=exc_info, stacklevel=2
    )


def error_type_name(error: BaseException) -> str:
    """Type name for a log line, naming a wrapped cause when there is one.

    An exception raised inside a Ray actor reaches the caller as a
    ``RayTaskError``, whose own type says nothing about what actually failed —
    so ``error_type=RayTaskError`` hides precisely the fact you are looking for.
    Ray keeps the original on ``.cause``; naming both gives
    ``RayTaskError[CachedActorError]``, which is greppable and says which
    handler should have caught it.

    Cheap to call and safe on any exception: no cause, or a cause of the same
    type, yields the plain name.
    """
    name = type(error).__name__
    cause = getattr(error, "cause", None)
    if cause is not None and type(cause) is not type(error):
        return f"{name}[{type(cause).__name__}]"
    return name


def elapsed_ms(start: float) -> float:
    """Milliseconds since ``start`` (a ``time.time()`` value), rounded."""
    return round((time.time() - start) * 1000, 2)
