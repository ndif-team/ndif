"""NDIF metric definitions: one callable class per thing we chart.

Each class is a thin, self-documenting wrapper over
:meth:`~ndif.common.providers.influx.InfluxProvider.write` that fixes an
InfluxDB *measurement* name and decides the tag/field split for that metric.
Call sites just do e.g.::

    ExecutionTimeMetric.update(
        model_key=..., api_key=..., request_id=..., exec_ms=..., status="completed",
    )

and the point is enqueued (non-blocking, fail-open). If Influx is disabled or
unreachable the call is a silent no-op — metrics are best-effort telemetry.

**Tags vs. fields.** Tags are indexed and meant for the dimensions you group
by / filter on in Grafana, so they must stay low-cardinality:

    service, environment   (added for every point by the provider)
    model_key              (bounded: the set of served models)
    api_key                (per-user metrics — assumed bounded to your key set)
    email                  (the api_key's owning user, resolved at ingress; a
                            function of api_key so it adds no series cardinality.
                            Absent when auth is off or the key has no user)
    status / stage / gpu_index / load_type   (small enumerations)

Deliberately *not* tags (kept as fields to bound cardinality): ``request_id`` /
``session_id`` / ``replica_id`` (unbounded ids) and ``ip_address`` /
``user_agent`` (client identity — unbounded / long strings). They're retrievable
per point but not group-by dimensions.

Fields carry the measured values plus high-cardinality context that we want to
*retrieve* but never group by (``request_id``, ``session_id``, ``replica_id``).
Keeping those out of the tag set is what stops the series count from exploding.

Field *types* are kept consistent per field name — byte counts are ints,
durations are floats — because InfluxDB rejects a point whose field type
conflicts with the existing series.
"""

from typing import Dict, Optional, Tuple

from .providers.influx import InfluxProvider


class Metric:
    """Base class: names a measurement and forwards to the Influx writer."""

    #: InfluxDB measurement name (the metric's "table"). Set by each subclass.
    measurement: str = ""

    @classmethod
    def _emit(
        cls,
        *,
        tags: Dict[str, object],
        fields: Dict[str, object],
    ) -> None:
        InfluxProvider.write(cls.measurement, tags, fields)


class ModelLoadTimeMetric(Metric):
    """How long it took to place a model's weights onto its GPUs.

    Emitted both for the initial disk load and for WARM->HOT restores, told
    apart by ``load_type`` (``"initial"`` vs. ``"from_cache"``).
    """

    measurement = "model_load_time"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        duration_ms: float,
        load_type: str = "initial",
        num_gpus: Optional[int] = None,
    ) -> None:
        cls._emit(
            tags={"model_key": model_key, "load_type": load_type},
            fields={"duration_ms": float(duration_ms), "num_gpus": num_gpus},
        )


class GPUMemMetric(Metric):
    """Extra GPU memory a single request consumed during execution.

    Measured per device from torch's peak-allocation tracker: the caller resets
    the peak counter and snapshots the baseline before running the request, then
    reads the peak afterward. ``extra_bytes = peak - baseline`` is the request's
    own activation/allocation footprint on top of the resident weights.

    Pass ``per_device`` as ``{gpu_index: (baseline_bytes, peak_bytes)}``; one
    point is emitted per device, tagged with ``gpu_index``.
    """

    measurement = "gpu_mem"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        request_id: str,
        per_device: Dict[int, Tuple[int, int]],
        api_key: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        for gpu_index, (baseline_bytes, peak_bytes) in per_device.items():
            cls._emit(
                tags={
                    "model_key": model_key,
                    "api_key": api_key,
                    "email": email,
                    "gpu_index": gpu_index,
                },
                fields={
                    "request_id": request_id,
                    "baseline_bytes": int(baseline_bytes),
                    "peak_bytes": int(peak_bytes),
                    "extra_bytes": int(peak_bytes - baseline_bytes),
                },
            )


class RequestSizeMetric(Metric):
    """Size of an incoming request's serialized interventions blob (bytes).

    This is the ingress metric, so it also carries the caller's identity —
    ``ip_address`` / ``user_agent``. These are **fields, not tags**: IP is
    effectively unbounded (and user-agent strings are long and versioned), so
    indexing them as tags would explode the series count. As fields they're still
    retrievable/filterable per request, just not efficient group-by dimensions.
    """

    measurement = "request_size"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        request_id: str,
        payload_bytes: int,
        api_key: Optional[str] = None,
        email: Optional[str] = None,
        session_id: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> None:
        cls._emit(
            tags={"model_key": model_key, "api_key": api_key, "email": email},
            fields={
                "request_id": request_id,
                "session_id": session_id,
                "ip_address": ip_address,
                "user_agent": user_agent,
                "payload_bytes": int(payload_bytes),
            },
        )


class RequestResponseSizeMetric(Metric):
    """Size of the result blob staged in the object store for a request (bytes)."""

    measurement = "response_size"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        request_id: str,
        response_bytes: int,
        compressed: bool = False,
        api_key: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        cls._emit(
            tags={"model_key": model_key, "api_key": api_key, "email": email},
            fields={
                "request_id": request_id,
                "response_bytes": int(response_bytes),
                "compressed": bool(compressed),
            },
        )


class ExecutionTimeMetric(Metric):
    """Time a request spent being run on the model actor (ms), split into phases.

    Three fields carry the split so a slow request is attributable to the right
    phase:

        deserialize_ms  rehydrating the traced block against the loaded model
        exec_ms         running the user's interventions (pure execution)
        upload_ms       staging the result blob in the object store

    ``status`` (``completed`` / ``error`` / ``timeout`` / ``cancelled``) is a
    tag so latency can be sliced by outcome — e.g. p95 of successful runs only.
    Any field may be absent (e.g. ``deserialize_ms`` is unknown if the request
    errored before deserialization finished; ``upload_ms`` only exists on a
    completed run) and is simply dropped.
    """

    measurement = "execution_time"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        request_id: str,
        status: str = "completed",
        exec_ms: Optional[float] = None,
        deserialize_ms: Optional[float] = None,
        upload_ms: Optional[float] = None,
        api_key: Optional[str] = None,
        email: Optional[str] = None,
        replica_id: Optional[str] = None,
    ) -> None:
        cls._emit(
            tags={
                "model_key": model_key,
                "api_key": api_key,
                "email": email,
                "status": status,
            },
            fields={
                "request_id": request_id,
                "replica_id": replica_id,
                "exec_ms": None if exec_ms is None else float(exec_ms),
                "deserialize_ms": (
                    None if deserialize_ms is None else float(deserialize_ms)
                ),
                "upload_ms": None if upload_ms is None else float(upload_ms),
            },
        )


class RequestStatusTimeMetric(Metric):
    """Time a request spent in a single lifecycle status (ms).

    Recorded centrally: every time a request's status changes (see
    ``BackendRequestModel.respond``), one point is emitted for the status it was
    *leaving*, tagged with that ``status``. So the request walking
    ``SENT -> received -> queued -> dispatched -> running -> completed`` produces
    one point per hop — ``status="SENT"`` carries the client->server transit
    (recorded at ingress from the client's timestamp), ``status="queued"`` the
    queue wait, ``status="running"`` the execution time, etc. — and a dashboard
    can attribute end-to-end latency to each phase without any per-call-site
    instrumentation.
    """

    measurement = "status_time"

    @classmethod
    def update(
        cls,
        *,
        model_key: str,
        request_id: str,
        status: str,
        duration_ms: float,
        api_key: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        cls._emit(
            tags={
                "model_key": model_key,
                "api_key": api_key,
                "email": email,
                "status": status,
            },
            fields={
                "request_id": request_id,
                "duration_ms": float(duration_ms),
            },
        )
