import os

from . import Metric


class QueueStateMetric(Metric):
    """One point per live Processor — the dispatcher's per-model queue view.

    Captures, for each model the dispatcher currently has a Processor for,
    how many requests are waiting, what lifecycle state the Processor is in,
    and how many of its replicas are mid-execution. Emitted by the Dispatcher
    on a periodic heartbeat *and* immediately on any queue change (enqueue,
    status transition, job start/end), so the series tracks the live queue
    closely without missing short-lived bursts between heartbeats.

    Liveness is implicit, exactly as with ``deployment_state``: the dispatcher
    drops a Processor from its registry on eviction/cancellation, so it simply
    stops being emitted. "Right now" queries should use a short lookback window
    (one or two heartbeat intervals) rather than a wide range, and read the
    last point per ``model_key``.

    Tags are the (low-cardinality) series identity; fields carry the counts
    and durations.
    """

    name: str = "queue_state"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        status: str,
        pinned: bool,
        queue_length: int,
        num_replicas: int,
        num_busy_replicas: int,
        busy: bool,
        status_age_seconds: float,
        longest_running_seconds: float,
    ) -> None:
        # Short-circuit before building the Point if InfluxDB isn't configured
        # (the base class also no-ops, but Point isn't importable then).
        if cls.client is None and os.getenv("INFLUXDB_ADDRESS") is None:
            return

        from influxdb_client import Point

        point = (
            Point(cls.name)
            .tag("model_key", model_key)
            .tag("status", status)
            .tag("pinned", str(pinned).lower())
            .field("queue_length", int(queue_length))
            .field("num_replicas", int(num_replicas))
            .field("num_busy_replicas", int(num_busy_replicas))
            .field("busy", int(bool(busy)))
            .field("status_age_seconds", float(status_age_seconds))
            .field("longest_running_seconds", float(longest_running_seconds))
        )

        super().update(point)


class QueueJobMetric(Metric):
    """One point per *in-flight* job — a single busy replica's current request.

    The live counterpart to ``request_execution_time`` (which records the
    *final* duration of every completed job). This one is sampled while a job
    is still running, so a dashboard can show "what is executing right now and
    for how long" per model/replica. It is deliberately **not** emitted on
    completion — total durations already live in ``request_execution_time``, so
    re-emitting them here would duplicate that series.

    ``running_seconds`` is the wall-clock age of the in-flight request at
    snapshot time; ``current_request_id`` is a field (not a tag) because
    request IDs are unbounded cardinality.
    """

    name: str = "queue_job"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        replica_id: str,
        current_request_id: str,
        running_seconds: float,
    ) -> None:
        if cls.client is None and os.getenv("INFLUXDB_ADDRESS") is None:
            return

        from influxdb_client import Point

        point = (
            Point(cls.name)
            .tag("model_key", model_key)
            .tag("replica_id", replica_id)
            .field("current_request_id", current_request_id)
            .field("running_seconds", float(running_seconds))
        )

        super().update(point)
