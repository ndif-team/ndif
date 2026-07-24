"""Loki provider: ship the ``ndif`` logs to Grafana Loki, fail-open.

Log shipping is handled by the ``python-logging-loki`` package
(``logging_loki.LokiQueueHandler``): a ``QueueHandler`` whose background
listener thread owns the HTTP pushes, so ``emit`` never blocks the caller
(safe from the asyncio event loops). We wrap it only to:

  * format each record as a **structured JSON line** (message + host/source
    context + the caller's ``extra={...}`` fields + split exception), shared
    with the console formatter via :mod:`ndif.common.logging_setup`;
  * promote ``model_key`` to a Loki **stream label** (per-record, via
    ``record.tags``);
  * bound the in-memory queue so a Loki outage drops records instead of
    growing memory without limit.

**Opt-in / lazy**: nothing happens unless ``NDIF_LOKI_URL`` is set. Only then
is ``logging_loki`` imported and the handler attached — so an install that
never points at a Loki (and may not even have the package) pays nothing and
imports nothing. If the package is missing or the handler fails to build, we
log a warning and stay console-only (fail-open).

Static labels are ``service`` / ``environment``; ``logging_loki`` adds
``logger`` (= the ndif sub-logger, e.g. ``ndif.queue.replica``) and
``severity`` (= level) automatically; ``model_key`` is added per-record.
Everything else (request id, host, timings, message) lives in the JSON *line*,
queryable with Loki's ``| json`` pipeline but not indexed.

Like the other providers this is a classmethod singleton; :meth:`connect` is
idempotent so every process entry point can call it without coordinating.
"""

import json
import logging
import queue
from typing import Any, ClassVar, Dict, Optional

from ..logging_setup import (
    configure_console,
    exception_fields,
    source_context,
    structured_fields,
)
from .base import Provider

logger = logging.getLogger("ndif")

# Bounded-cardinality fields promoted from the JSON line to a Loki stream label.
# ``model_key`` is bounded (number of served models); request/session ids are
# NOT (unbounded) and stay in the line.
_LABEL_KEYS = ("model_key",)


class _JsonLineFormatter(logging.Formatter):
    """Render a record as the structured JSON line Loki stores.

    Same field extraction as the console formatter, so the two never disagree:
    message + host/code-location context + the caller's ``extra`` fields (last,
    so an explicit extra wins) + split exception info.
    """

    def format(self, record: logging.LogRecord) -> str:
        line: Dict[str, Any] = {
            "message": record.getMessage(),
            **source_context(record),
            **structured_fields(record),
            **exception_fields(record),
        }
        return json.dumps(line, default=str)


class _LabelFilter(logging.Filter):
    """Promote bounded-cardinality fields (``model_key``) to stream labels.

    ``logging_loki`` reads ``record.tags`` for per-record labels; this filter
    copies the promoted fields there. Runs on the handler, before the record is
    enqueued, so the label survives to the listener thread.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        tags = dict(getattr(record, "tags", None) or {})
        for key in _LABEL_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                tags[key] = str(value)
        if tags:
            record.tags = tags
        return True


def _build_handler(url: str, tags: Dict[str, str], queue_max: int, level: str):
    """Construct the bounded, JSON-formatting LokiQueueHandler.

    Imports ``logging_loki`` lazily (only reached when ``NDIF_LOKI_URL`` is set)
    and raises if it isn't installed — the caller turns that into a fail-open
    console-only warning.
    """
    from logging_loki import LokiQueueHandler

    class _BoundedLokiQueueHandler(LokiQueueHandler):
        """Drop on a full queue instead of erroring — a Loki outage must not
        back up memory or spam stderr via ``handleError``."""

        def enqueue(self, record: logging.LogRecord) -> None:
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                pass

    handler = _BoundedLokiQueueHandler(
        queue.Queue(maxsize=queue_max),
        url=url,
        tags=tags,
        version="1",  # target /loki/api/v1/push
    )
    handler.setLevel(level)
    handler.setFormatter(_JsonLineFormatter())
    handler.addFilter(_LabelFilter())

    # Fail-open: when Loki is unreachable the inner LokiHandler's emit() calls
    # handleError(), which dumps a full traceback to stderr *per record*. Silence
    # it — a Loki outage must drop logs quietly, not flood the console/disk.
    inner = getattr(handler, "handler", None)
    if inner is not None:
        inner.handleError = lambda record: None  # type: ignore[method-assign]

    return handler


class LokiProvider(Provider):
    """Installs (once per process) the Loki handler on the ``ndif`` logger.

    A no-op for Loki unless ``NDIF_LOKI_URL`` is set; the readable console is
    configured regardless.
    """

    CONFIG = {
        # Empty (the default) disables Loki entirely — the package is never
        # imported. Set it (e.g. http://loki:3100/loki/api/v1/push) to enable.
        "url": ("NDIF_LOKI_URL", "", str),
        # Static stream labels. ``service`` (api / ray) is set per process by
        # the start scripts; ``environment`` distinguishes prod / staging / dev.
        "service": ("NDIF_SERVICE", "unknown", str),
        "environment": ("NDIF_ENVIRONMENT", "dev", str),
        # Only records at/above this level are shipped (console keeps its own).
        "level": ("NDIF_LOKI_LEVEL", "INFO", str),
        # Bound the in-memory queue so a Loki outage drops rather than grows.
        "queue_max": ("NDIF_LOKI_QUEUE_MAX", 10000, int),
    }

    url: str
    service: str
    environment: str
    level: str
    queue_max: int

    handler: ClassVar[Optional[logging.Handler]] = None

    @classmethod
    def connect(cls) -> None:
        """Configure the readable console and, if ``NDIF_LOKI_URL`` is set,
        attach the Loki handler. Idempotent.

        Runs at import (bottom of this module), so a process that imports the
        provider connects automatically. **Fork caveat:** the handler owns a
        background shipper thread, and threads don't survive ``fork()`` — so a
        process must not connect *before* forking children that will ship logs
        (the child would inherit a dead handler). The gunicorn master therefore
        avoids importing this module until after it forks; see the ``post_fork``
        hook in ``gunicorn_conf``.

        The console is always (re)configured — nice, structured output is worth
        having even without Loki. Loki is added on top only when a URL is set;
        console logging is never removed, only added to.
        """
        # Readable console on the `ndif` logger (also stops root-handler
        # duplication). Runs regardless of whether Loki is enabled.
        configure_console("ndif")

        if not cls.url or cls.handler is not None:
            return

        tags = {"service": cls.service, "environment": cls.environment}
        try:
            handler = _build_handler(cls.url, tags, cls.queue_max, cls.level)
        except ImportError:
            logger.warning(
                "NDIF_LOKI_URL is set but python-logging-loki is not installed; "
                "logs stay console-only",
                extra={"loki_url": cls.url},
            )
            return
        except Exception:
            logger.warning(
                "Failed to initialize the Loki handler; logs stay console-only",
                exc_info=True,
            )
            return

        ndif_logger = logging.getLogger("ndif")
        ndif_logger.addHandler(handler)
        # Ensure records at our level actually reach the handler.
        if ndif_logger.level == logging.NOTSET or ndif_logger.level > handler.level:
            ndif_logger.setLevel(handler.level)

        cls.handler = handler
        logger.info(
            "Loki telemetry enabled",
            extra={"loki_url": cls.url, "loki_service": cls.service},
        )

    @classmethod
    def connected(cls) -> bool:
        return cls.handler is not None

    @classmethod
    def disconnect(cls) -> None:
        if cls.handler is None:
            return
        logging.getLogger("ndif").removeHandler(cls.handler)
        # Stop the LokiQueueHandler's background listener thread if present.
        listener = getattr(cls.handler, "listener", None)
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        cls.handler.close()
        cls.handler = None


LokiProvider.from_env()
LokiProvider.connect()
