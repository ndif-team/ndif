"""InfluxDB provider: ship NDIF metrics to InfluxDB 2.x, fail-open.

Where :class:`~ndif.common.providers.loki.LokiProvider` carries *logs* (one
record per event, human-readable), this provider carries *metrics* — numeric
time series (durations, byte counts, memory) you chart and alert on in Grafana.
The metric *definitions* (what fields/tags each one carries) live in
:mod:`ndif.common.metrics`; this module is just the sink they write through.

Design constraints (mirroring the Loki provider):

  * **Fail-open** — Influx being down, slow, or misconfigured must never break a
    request or crash a service. Every write path is wrapped; on failure the
    point is dropped and we move on. If the ``influxdb-client`` package isn't
    importable at all (e.g. a minimal local checkout), the provider simply
    stays disconnected and every ``write`` is a no-op.
  * **Non-blocking** — writes go through the client's *batching* write API,
    which enqueues the point onto an in-memory buffer and flushes from a
    background thread. ``write`` never awaits network I/O, so it is safe to call
    from the asyncio event loop (API workers, dispatcher) and from the model
    actor's execution thread alike.

InfluxDB's data model splits a point's dimensions into **tags** (indexed, meant
for low-cardinality group-by / filter) and **fields** (the actual values, plus
any high-cardinality context). The metric classes decide that split; the
provider only merges in the process-wide base tags (``service`` /
``environment``) and hands the point to the batching writer.

Like the other providers this is a classmethod singleton; :meth:`connect` is
idempotent so every process entry point (each uvicorn worker, each Ray actor)
can call it without coordinating.
"""

import atexit
import logging
import time
from typing import Any, ClassVar, Dict, Optional

from .base import Provider

logger = logging.getLogger("ndif")

# The client is an optional import: metrics are best-effort telemetry, so a
# checkout without ``influxdb-client`` installed should degrade to "metrics
# disabled" rather than fail to import the whole common package.
try:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    from influxdb_client.client.write_api import WriteOptions

    _HAS_CLIENT = True
except Exception:  # pragma: no cover - exercised only where the dep is absent
    InfluxDBClient = None  # type: ignore[assignment]
    Point = None  # type: ignore[assignment]
    WritePrecision = None  # type: ignore[assignment]
    WriteOptions = None  # type: ignore[assignment]
    _HAS_CLIENT = False


def _boolish(raw: str) -> bool:
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


class InfluxProvider(Provider):
    """Installs (once per process) a batching InfluxDB write client."""

    CONFIG = {
        "url": ("NDIF_INFLUX_URL", "http://localhost:8086", str),
        "token": ("NDIF_INFLUX_TOKEN", "", str),
        "org": ("NDIF_INFLUX_ORG", "ndif", str),
        "bucket": ("NDIF_INFLUX_BUCKET", "metrics", str),
        "enabled": ("NDIF_INFLUX_ENABLED", True, _boolish),
        # Base tags. ``service`` (api / ray) is set per process by the start
        # scripts; ``environment`` distinguishes prod / staging / dev. These two
        # are attached to every point so a Grafana dashboard can scope by them.
        "service": ("NDIF_SERVICE", "unknown", str),
        "environment": ("NDIF_ENVIRONMENT", "dev", str),
        # Batching writer tuning. batch_size / flush_interval_ms are a "flush
        # when either threshold is hit" pair; timeout_ms bounds each HTTP write.
        "batch_size": ("NDIF_INFLUX_BATCH_SIZE", 500, int),
        "flush_interval_ms": ("NDIF_INFLUX_FLUSH_INTERVAL_MS", 1000, int),
        "timeout_ms": ("NDIF_INFLUX_TIMEOUT_MS", 10000, int),
    }

    url: str
    token: str
    org: str
    bucket: str
    enabled: bool
    service: str
    environment: str
    batch_size: int
    flush_interval_ms: int
    timeout_ms: int

    client: ClassVar[Optional[Any]] = None
    write_api: ClassVar[Optional[Any]] = None

    @classmethod
    def connect(cls) -> None:
        """Construct the batching write client. Idempotent, fail-open.

        Runs at import (bottom of this module), so a process that imports the
        provider connects automatically. **Fork caveat:** the batching writer
        owns a background flush thread, and threads don't survive ``fork()`` — so
        a process must not connect *before* forking children that will emit
        metrics (the child would inherit a dead writer). The gunicorn master
        therefore avoids importing this module until after it forks; see the
        ``post_fork`` hook in ``gunicorn_conf``.

        A construction failure (bad URL, missing dependency, unreachable server)
        is swallowed — metrics stay disabled and the service runs normally.
        """
        if not cls.enabled or not _HAS_CLIENT or cls.client is not None:
            return

        try:
            client = InfluxDBClient(
                url=cls.url,
                token=cls.token,
                org=cls.org,
                timeout=cls.timeout_ms,
            )
            write_api = client.write_api(
                write_options=WriteOptions(
                    batch_size=cls.batch_size,
                    flush_interval=cls.flush_interval_ms,
                    # Fail-open, non-blocking shutdown: don't retry a failed
                    # flush. Retries would (a) block ``close()`` at shutdown
                    # while it drains a backlog against a down server and (b)
                    # let the buffer grow unbounded. Metrics are best-effort, so
                    # a failed batch is simply dropped.
                    max_retries=0,
                ),
                # A failed batch flush is logged at debug, never raised (the
                # batching writer runs on its own thread anyway).
                error_callback=cls._on_write_error,
            )
        except Exception:
            logger.debug("InfluxDB telemetry unavailable", exc_info=True)
            return

        cls.client = client
        cls.write_api = write_api
        atexit.register(cls.disconnect)

        logger.info(
            "InfluxDB telemetry enabled",
            extra={
                "influx_url": cls.url,
                "influx_org": cls.org,
                "influx_bucket": cls.bucket,
                "influx_service": cls.service,
            },
        )

    @staticmethod
    def _on_write_error(conf: Any, data: Any, exception: Exception) -> None:
        # Don't log at INFO+: a flapping Influx would otherwise spam every flush.
        logger.debug("InfluxDB write failed; dropping batch", exc_info=exception)

    @classmethod
    def _base_tags(cls) -> Dict[str, str]:
        return {"service": cls.service, "environment": cls.environment}

    @classmethod
    def write(
        cls,
        measurement: str,
        tags: Dict[str, Any],
        fields: Dict[str, Any],
    ) -> None:
        """Enqueue one point (non-blocking, fail-open).

        ``tags`` are stringified and merged over the process base tags;
        ``fields`` carry the values. ``None`` values in either map are dropped so
        optional context (a missing api_key, an absent replica_id) doesn't
        create empty series. A point with no fields is skipped — InfluxDB
        rejects fieldless points, and there's nothing to record anyway.
        """
        if cls.write_api is None:
            return

        clean_fields = {k: v for k, v in fields.items() if v is not None}
        if not clean_fields:
            return

        try:
            point = Point(measurement)
            # Stamp an explicit nanosecond timestamp. Without one, InfluxDB
            # assigns the server ingestion time at flush, so every point in a
            # batch shares a timestamp — and points that land on the same
            # measurement+tag-set+timestamp overwrite each other (silently
            # dropping high-frequency metrics). time_ns() gives each point a
            # distinct time and preserves event ordering.
            point.time(time.time_ns(), WritePrecision.NS)
            for key, value in {**cls._base_tags(), **tags}.items():
                if value is not None:
                    point.tag(key, str(value))
            for key, value in clean_fields.items():
                point.field(key, value)
            cls.write_api.write(bucket=cls.bucket, record=point)
        except Exception:
            # Never let a metric write surface into the caller.
            logger.debug("InfluxDB point build/enqueue failed", exc_info=True)

    @classmethod
    def connected(cls) -> bool:
        return cls.write_api is not None

    @classmethod
    def disconnect(cls) -> None:
        """Flush and close the writer. Idempotent."""
        if cls.client is None:
            return
        try:
            if cls.write_api is not None:
                cls.write_api.close()  # flushes any buffered points
            cls.client.close()
        except Exception:
            pass
        cls.write_api = None
        cls.client = None


InfluxProvider.from_env()
InfluxProvider.connect()
