---
title: Telemetry Internals
one_liner: How logging, structured events, and Influx metrics are wired — logger tree, the event() API, every metric class, the fail-open behavior of both sinks, and the fork-safe connect points.
tags: [internals, dev, telemetry]
related: [docs/operating/observability.md, docs/developing/providers.md, docs/reference/schemas.md, docs/reference/env-vars.md, docs/developing/api-service.md, docs/developing/model-actor.md, docs/runbooks/trace-a-users-failed-job.md]
sources: [src/ndif/common/logging_setup.py, src/ndif/common/telemetry.py, src/ndif/common/metrics.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/influx.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/api/app.py, src/ndif/common/schema/request.py]
---

# Telemetry Internals

## What this covers

Three modules in `src/ndif/common/` make up the observability layer, writing into
two sinks (`src/ndif/common/providers/loki.py`, `src/ndif/common/providers/influx.py`):

- `logging_setup.py` — the console handler and the shared record-field extraction.
- `telemetry.py` — `event()`, the structured-logging front door.
- `metrics.py` — one class per numeric time series, written through `InfluxProvider`.

Two constraints shape all of it, and this page is the home for both. **Fail-open:**
Loki or InfluxDB being down, missing or unconfigured never breaks a request — every
sink degrades to a no-op. **Fork-safe:** both sinks own a background shipper thread,
and threads don't survive `fork()`, so *where* a process connects them is part of
the design. Running the sinks is [Observability](../operating/observability.md);
their variables are in [Environment variables](../reference/env-vars.md).

## Logging

### The logger tree

Everything hangs off one logger, `ndif`. Handlers attach there and nowhere else;
every module takes a dotted child so records carry a meaningful `logger` label.

| Logger | Used by |
|---|---|
| `ndif` | the providers (the object-store provider has no logger) |
| `ndif.api` | `src/ndif/services/api/app.py`, `src/ndif/services/api/auth.py` |
| `ndif.request` | request lifecycle transitions (`src/ndif/common/schema/request.py`) |
| `ndif.queue.dispatcher` / `.processor` / `.replica` | the matching modules under `src/ndif/services/api/queue/` |
| `ndif.controller` | the controller and every `cluster/*.py` module |
| `ndif.modeling` | the model actor, its helpers, and the tensor-parallel package |
| `ndif.dashboard.reconcile` | the dashboard's reconcile job |

### `configure_console` and `NDIF_LOG_LEVEL`

`src/ndif/common/logging_setup.py` `configure_console(name="ndif")` is idempotent
and does three things:

1. `propagate = False`, so records aren't also printed by a root handler
   (gunicorn's, or the `basicConfig` the dispatcher's `__main__` installs).
2. Applies `NDIF_LOG_LEVEL` (default `INFO`; unparseable → `INFO`) unless the
   logger is already lower, so a Loki handler that lowered it isn't undone.
3. Installs a stdout `StreamHandler` with `ConsoleFormatter`, marked
   `_ndif_console` so a second call adds nothing.

It is called from exactly one place, `LokiProvider.connect` — *before* the Loki URL
check, so importing the Loki provider is what gives a process readable logs, with or
without Loki.

### Record field extraction

Three helpers in `logging_setup.py`, shared by the console and Loki formatters so
the two never disagree about what a record means:

| Function | Returns |
|---|---|
| `structured_fields(record)` | what the caller passed as `extra={...}`: everything on `record.__dict__` that isn't a stdlib `LogRecord` attribute, doesn't start with `_`, and isn't `tags` (a Loki control attribute) |
| `source_context(record)` | `host` (`HOSTNAME`, resolved once), `pid`, `thread`, `func`, `file`, `line` |
| `exception_fields(record)` | `exception_type`, `exception_message`, `stacktrace` — split so a dashboard groups by type without regexing a traceback |

`ConsoleFormatter` renders all of it as one line, truncating values past
`_MAX_VALUE_LEN` (200) on the console only:

```
2026-07-06 12:00:01 INFO    [ndif.queue.replica] request completed model_key=gpt2 request_id=ab12 exec_ms=1200.5 (replica.py:309)
```

### How Loki shipping is attached

After the console, `LokiProvider.connect` returns unless `NDIF_LOKI_URL` is set.
Otherwise it lazily imports `logging_loki`, builds the handler (`_build_handler`),
adds it to `ndif`, and lowers the logger's level if needed so records reach it. The
handler has three customizations:

- **JSON line** — `_JsonLineFormatter` emits
  `{message, ...source_context, ...structured_fields, ...exception_fields}`.
- **Per-record label** — `_LabelFilter` copies `model_key` (the only bounded field)
  onto `record.tags`, which becomes a Loki stream label. Request and session ids
  stay in the line (`| json`), since as labels they would explode cardinality.
- **Bounded, quiet queue** — `_BoundedLokiQueueHandler` drops on a full queue, and
  the inner handler's `handleError` is a no-op.

Static stream labels are `service` and `environment`; `logging_loki` adds `logger`
and `severity`.

## `event()` — structured logging

`src/ndif/common/telemetry.py` `event`. Use it instead of `logger.info(msg,
extra={...})`:

```python
from ndif.common.telemetry import elapsed_ms, event

event(logger, "request enqueued", model_key=self.model_key,
      request_id=request.id, queue_size=self.queue.qsize())
```

It drops `None`-valued fields and keys that collide with a reserved `LogRecord`
attribute (`logging` raises `KeyError` on those — `_clean`); passes `stacklevel=2`
so `func`/`file`/`line` point at the caller; and takes `level=` and `exc_info=` as
keyword-only knobs (`exc_info=True` produces the split exception fields).
`elapsed_ms(start)` is milliseconds since a `time.time()` value, rounded to two
decimals; every `*_ms` field comes from it.

Field names are conventions, not a schema, and Grafana queries key off them — keep
them stable: `model_key`, `replica_id`, `request_id`, `session_id`, `api_key`,
`email`, `stage`, `prev_stage`, `prev_stage_ms`, `duration_ms`, `exec_ms`,
`queue_size`, `replicas_before`, `replicas_after`, `error_type`.

**`error_type` names the real cause.** Where an exception may have crossed the Ray
boundary it is built by `error_type_name`, which reads a `RayTaskError`'s `.cause`
and renders `RayTaskError[CachedActorError]` — query the bracketed part; a bare
`RayTaskError` means the cause was absent. The model actor's `model execution
errored` event goes through the deployment's `error_name` seam: a sandboxed failure
arrives as a `RunnerError`, and `SandboxHost.error_name` records the type name the
runner shipped, so a user mistake counts the same sandboxed or not.

The densest emitter is `src/ndif/common/schema/request.py`
`BackendRequestModel._advance_status`: one event per lifecycle transition carrying
`stage`, `prev_stage` and `prev_stage_ms`, enough to rebuild a request's timeline
from logs alone.

## Metrics

`src/ndif/common/metrics.py` is one class per chartable thing. `Metric` fixes an
Influx *measurement* name and forwards to `InfluxProvider.write` via `_emit`; each
subclass's `update()` is keyword-only and decides the tag/field split.

**Tags vs fields.** Tags are indexed group-by dimensions and stay low-cardinality:
`service` and `environment` (added to every point by the provider), `model_key`,
`api_key`, `email` (a function of `api_key`, so no extra series), and small
enumerations (`status`, `load_type`, `gpu_index`). Unbounded values — `request_id`,
`session_id`, `replica_id`, `ip_address`, `user_agent` — are **fields**. Field
*types* must stay consistent per name (bytes `int`, durations `float`): InfluxDB
rejects a point that conflicts with an existing series, hence the explicit casts.

| Metric class | Measurement | Tags | Fields | Emitted by |
|---|---|---|---|---|
| `ModelLoadTimeMetric` | `model_load_time` | `model_key`, `load_type` (`initial` \| `from_cache`) | `duration_ms`, `num_gpus` | `BaseModelDeployment.load_from_disk` and `from_cache`; the tensor-parallel actor's load |
| `GPUMemMetric` | `gpu_mem` | `model_key`, `api_key`, `email`, `gpu_index` | `request_id`, `baseline_bytes`, `peak_bytes`, `extra_bytes` | `BaseModelDeployment.run`, one point per device |
| `RequestSizeMetric` | `request_size` | `model_key`, `api_key`, `email` | `request_id`, `session_id`, `ip_address`, `user_agent`, `payload_bytes` | `create_request` in `src/ndif/services/api/app.py`, at ingress |
| `RequestResponseSizeMetric` | `response_size` | `model_key`, `api_key`, `email` | `request_id`, `response_bytes`, `compressed` | `BaseModelDeployment.prepare_result` |
| `ExecutionTimeMetric` | `execution_time` | `model_key`, `api_key`, `email`, `status` (`completed` \| `error` \| `timeout` \| `cancelled` \| `preempted`) | `request_id`, `replica_id`, `exec_ms`, `deserialize_ms`, `upload_ms` | `BaseModelDeployment.report` |
| `RequestStatusTimeMetric` | `status_time` | `model_key`, `api_key`, `email`, `status` | `request_id`, `duration_ms` | `BackendRequestModel._advance_status` (every transition); `create_request` (the synthetic `SENT` hop) |

- **`execution_time` splits the run into phases** — `deserialize_ms`, `exec_ms`,
  `upload_ms` — so a slow request is attributable. Absent fields are dropped (an
  error before deserialize has no `deserialize_ms`). `replica_id` is declared but
  `report()` never passes it.
- **`status_time` is the latency breakdown, recorded centrally.** Each transition
  emits one point for the status being *left*, so `SENT → RECEIVED → QUEUED →
  DISPATCHED → RUNNING → COMPLETED` yields one point per hop: `SENT` is
  client→server transit (from the `ndif-timestamp` header, skipped when it looks
  like clock skew), `QUEUED` is queue wait, `RUNNING` is execution.

### Adding a new metric

1. Add a `Metric` subclass in `src/ndif/common/metrics.py` with a new snake_case
   `measurement` and a docstring saying what it measures and why the tag/field
   split is what it is:

   ```python
   class SandboxStartupMetric(Metric):
       """How long acquiring a fresh sandbox runner took (ms)."""

       measurement = "sandbox_startup"

       @classmethod
       def update(
           cls, *, model_key: str, request_id: str, duration_ms: float,
           api_key: Optional[str] = None, email: Optional[str] = None,
       ) -> None:
           cls._emit(
               tags={"model_key": model_key, "api_key": api_key, "email": email},
               fields={"request_id": request_id, "duration_ms": float(duration_ms)},
           )
   ```

2. Keep arguments keyword-only, default optional context to `None` (dropped by the
   provider), and cast field values to a fixed type.
3. Put anything unbounded in `fields`, never `tags`.
4. Emit from the call site, timing with `elapsed_ms`.
5. Make sure the emitting process connects `InfluxProvider` ([Process
   wiring](#process-wiring)) — an actor that never imported it records nothing.
6. Add a row to the table above and to [Observability](../operating/observability.md).

## Fail-open behavior

Nothing here can take down a request.

| Condition | Result |
|---|---|
| `NDIF_LOKI_URL` unset | Loki never imported; console only |
| `python-logging-loki` missing, or the handler fails to build | one WARNING, console only |
| Loki unreachable at runtime | records dropped silently: the queue drops when full, `handleError` is neutered (stock behavior prints a traceback per record) |
| `influxdb-client` missing (`_HAS_CLIENT`), `NDIF_INFLUX_URL` unset, or `NDIF_INFLUX_ENABLED` false | `connect()` returns immediately; every `write` is a no-op |
| Influx client fails to construct | swallowed, logged at DEBUG; metrics stay off |
| Influx unreachable at flush | batch dropped; `_on_write_error` logs at DEBUG only, and `max_retries=0` keeps the buffer bounded and `close()` non-blocking |
| A point with no non-`None` fields | skipped (InfluxDB rejects fieldless points) |

`InfluxProvider.connect` also silences the `influxdb_client` library's own logger,
which would otherwise log every failed flush at ERROR — and in a model actor, stderr
during a request reaches the user as `LOG` lines. `InfluxProvider.write` never
awaits network I/O: the point joins the client's in-memory batch and a background
thread flushes it, so it is safe from an event loop and from the execution thread
alike. Each point gets an explicit `time.time_ns()` timestamp; without one, points in
a batch share the ingestion time and same-tag-set points overwrite each other.

## Process wiring

Both sinks connect at *import* and own a background thread, so the import point is
load-bearing: **a process must not import them before it forks children that
emit.** A child would inherit a dead shipper.

### The API (gunicorn)

`src/ndif/services/api/gunicorn_conf.py` follows that rule:

- The config module imports neither the providers nor the dispatcher at module
  level (the dispatcher would pull them in transitively).
- `post_fork` imports `ndif.common.providers.influx` and `.loki` **in each worker**,
  after the fork, so every worker gets its own live threads.
- `on_starting` launches the queue dispatcher with a **spawn** context; its target
  `_run_dispatcher` imports everything inside the fresh interpreter.

This assumes gunicorn's default `preload_app = False`; preloading would import the
app and its providers into the master before the fork. Run standalone, the
dispatcher's `__main__` does the same imports itself.

### Ray

`src/ndif/services/ray/start.sh` exports `NDIF_SERVICE=ray` (unless already set)
before `ray start`, so the raylet and its actors inherit it. Each actor connects in
its own `__init__`: the controller connects **Loki only** (it emits events, no
metrics); `BaseModelDeployment.__init__` connects **both**. Ray workers inherit only
the node's ambient environment, so the controller ships provider config into each
model actor's `runtime_env` and overrides `NDIF_SERVICE` to `model` — see
[Providers](providers.md#config-propagation).

## Gotchas

- **`NDIF_SERVICE` means two things.** It is the telemetry `service` tag *and* the
  CLI's service selector — `ndif start` reads it as a space/comma-separated list
  (`src/ndif/cli/service.py` `env_services`). Compose sets one name per container,
  which satisfies both; a list would also relabel that container's telemetry.
- **A process that never imports the providers has no console formatting and no
  telemetry.** A new entry point (a script, a Ray actor) must import
  `ndif.common.providers.loki` — and `.influx` if it emits metrics.
- **A metric's field types are permanent per series.** Cast in `update()`.

## Related

[Observability](../operating/observability.md) for running Loki, InfluxDB and
Grafana; [Providers](providers.md) for the provider pattern;
[Schemas](../reference/schemas.md) for `_advance_status`;
[Trace a user's failed job](../runbooks/trace-a-users-failed-job.md) for using
`request_id` / `email` across logs and metrics.
