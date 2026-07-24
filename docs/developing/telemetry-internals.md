---
title: Telemetry Internals
one_liner: How logging, structured events, and Influx metrics are wired — logger tree, the event() API, every metric class, and the fork-safe connect points.
tags: [internals, dev, telemetry]
related: [docs/operating/observability.md, docs/developing/providers.md, docs/reference/schemas.md, docs/reference/env-vars.md, docs/developing/api-service.md, docs/developing/model-actor.md, docs/runbooks/trace-a-users-failed-job.md]
sources: [src/ndif/common/logging_setup.py, src/ndif/common/telemetry.py, src/ndif/common/metrics.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/influx.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/api/app.py, src/ndif/common/schema/request.py]
---

# Telemetry Internals

## What this covers

Three modules in `src/ndif/common/` make up NDIF's observability layer:

- `logging_setup.py` — the console handler and the shared record-field extraction.
- `telemetry.py` — `event()`, the structured-logging front door.
- `metrics.py` — one class per numeric time series, written through `InfluxProvider`.

Two constraints shape all of it. **Fail-open:** neither Loki nor InfluxDB being
down, missing, or unconfigured may break a request — every sink degrades to a
no-op. **Fork-safe:** both sinks own a background shipper thread, and threads
don't survive `fork()`, so *where* in a process's lifecycle they connect is part
of the design, not an accident.

Logs go to Loki (one line per event, queryable with `| json`), metrics go to
InfluxDB. The [providers page](./providers.md) covers the sinks themselves; this
page covers what is emitted into them.

## Logging

### The logger tree

Everything hangs off one logger named `ndif`. Handlers are attached there and
nowhere else; every module takes a dotted child so records carry a meaningful
`logger` label without needing their own handler.

| Logger | Used by |
|---|---|
| `ndif` | every provider (`providers/base.py:20`, `redis.py:19`, `ray.py:20`, `objectstore.py:30`, `postgres.py:34`, `influx.py:40`, `loki.py:45`) |
| `ndif.api` | the FastAPI app and auth (`services/api/app.py:48`, `auth.py:40`) |
| `ndif.request` | request lifecycle transitions (`common/schema/request.py:21`) |
| `ndif.queue.dispatcher` | `services/api/queue/dispatcher.py:49` |
| `ndif.queue.processor` | `services/api/queue/processor.py:38` |
| `ndif.queue.replica` | `services/api/queue/replica.py:43` |
| `ndif.controller` | `controller.py:25` and every `cluster/*.py` module |
| `ndif.modeling` | the model actor and its helpers (`modeling/base.py:66`, `modeling/util.py:13`) |
| `ndif.dashboard.reconcile` | `services/dashboard/jobs/reconcile.py:48` |

### `configure_console` and `NDIF_LOG_LEVEL`

`configure_console(name="ndif")` (`logging_setup.py:117`) is idempotent and does
four things:

1. `logger.propagate = False` — so records aren't *also* printed by a root
   handler (gunicorn's, or the `logging.basicConfig(level=logging.INFO)` the
   dispatcher's `__main__` installs at `dispatcher.py:396`).
2. Reads `NDIF_LOG_LEVEL` (default `INFO`, upper-cased; unparseable falls back to
   `INFO`) and applies it — but **only if the logger's current level is NOTSET or
   higher** (`logging_setup.py:131`), so a Loki handler that already lowered it
   to DEBUG isn't undone.
3. Installs a stdout `StreamHandler` with `ConsoleFormatter`, at the same level,
   marked `_ndif_console = True` so a second call is a no-op (`:134`).

It is called from exactly one place: `LokiProvider.connect()` (`loki.py:177`) —
*before* the Loki URL check, so the readable console exists whether or not Loki
is configured. Importing the Loki provider is what gives a process nice logs.

### Record field extraction

Three helpers, shared by the console formatter and the Loki JSON formatter so the
two can never disagree about what a record means:

| Function | Returns |
|---|---|
| `structured_fields(record)` (`logging_setup.py:40`) | everything on `record.__dict__` that isn't a stdlib `LogRecord` attribute and doesn't start with `_` — i.e. exactly what the caller passed as `extra={...}`. `tags` is excluded too: the Loki provider uses it as a control attribute for per-record labels, not as data. |
| `source_context(record)` (`:49`) | `host` (module-level `HOSTNAME`, resolved once at import, `:26`), `pid`, `thread`, `func`, `file`, `line`. |
| `exception_fields(record)` (`:61`) | `exception_type`, `exception_message`, and the formatted `stacktrace` — split so a dashboard can group by type without regexing a traceback. Empty when there's no `exc_info`. |

`ConsoleFormatter` (`:75`) renders all of that as one line:

```
2026-07-06 12:00:01 INFO    [ndif.queue.replica] request completed model_key=gpt2 request_id=ab12 exec_ms=1200.5 (replica.py:281)
```

Values longer than `_MAX_VALUE_LEN = 200` are truncated on the console only
(`:88`) — the full value still ships to Loki.

### How Loki shipping is attached

`LokiProvider.connect()` (`loki.py:158`) configures the console, then returns
unless `NDIF_LOKI_URL` is set (`loki.py:179`). When it is, it lazily imports
`logging_loki`, builds a `LokiQueueHandler` subclass, adds it to the `ndif` logger
(`loki.py:200`), and lowers the logger's level if needed so records at the
handler's level reach it (`loki.py:202`). Three customizations
(`_build_handler`, `loki.py:90`):

- **JSON line** — `_JsonLineFormatter` (`loki.py:53`) emits
  `{message, ...source_context, ...structured_fields, ...exception_fields}`.
- **Per-record label** — `_LabelFilter` (`loki.py:71`) copies `model_key` (the
  only entry in `_LABEL_KEYS`) onto `record.tags`, which `logging_loki` turns
  into a Loki stream label. Request/session ids stay in the line: they're
  unbounded and would explode stream cardinality.
- **Bounded, quiet queue** — `_BoundedLokiQueueHandler.enqueue` drops on a full
  queue instead of raising (`loki.py:103`), and the inner handler's
  `handleError` is replaced with a no-op (`loki.py:124`) so a Loki outage
  doesn't dump a traceback to stderr *per record*.

Static stream labels are `service` and `environment`; `logging_loki` adds
`logger` and `severity` itself.

## `event()` — structured logging

`telemetry.py:44`. Use it instead of `logger.info(msg, extra={...})`:

```python
from ndif.common.telemetry import elapsed_ms, event

event(logger, "request enqueued", model_key=self.model_key,
      request_id=request.id, queue_size=self.queue.qsize())
```

What it adds over raw `logger.log`:

- `None`-valued fields are dropped, so optional ids don't clutter the line, as
  are keys colliding with a reserved `LogRecord` attribute — `logging` raises
  `KeyError` on those (`_clean`, `telemetry.py:35`; the reserved set at `:29`).
- `stacklevel=2` (`telemetry.py:63`), so `func`/`file`/`line` point at the real
  call site rather than at `event` itself.
- `level=` and `exc_info=` are keyword-only knobs; `exc_info=True` is what
  produces the split `exception_type` / `exception_message` / `stacktrace`.

`elapsed_ms(start)` (`telemetry.py:67`) — milliseconds since a `time.time()`
value, rounded to 2 decimals. Every `*_ms` field comes from it.

Field names are conventions, not a schema, and Grafana queries key off them —
keep them stable. Established ones: `model_key`, `replica_id`, `request_id`,
`session_id`, `api_key`, `email`, `stage`, `prev_stage`, `prev_stage_ms`,
`duration_ms`, `exec_ms`, `queue_size`, `replicas_before`, `replicas_after`,
`error_type`. The densest emitter is `BackendRequestModel._advance_status`
(`common/schema/request.py:121`): one event per lifecycle transition carrying
`stage`, `prev_stage`, and `prev_stage_ms` — enough to reconstruct a request's
whole timeline from logs alone.

## Metrics

`metrics.py` is one class per chartable thing. `Metric` (`metrics.py:45`) fixes
an Influx *measurement* name and forwards to `InfluxProvider.write` via `_emit`
(`:51`); each subclass's `update()` is keyword-only and decides the tag/field
split.

**Tags vs fields.** Tags are indexed and are your Grafana group-by dimensions,
so they stay low-cardinality: `service` and `environment` (added to every point
by the provider, `influx.py:195`), `model_key`, `api_key`, `email`, and small
enumerations (`status`, `load_type`, `gpu_index`). Unbounded values —
`request_id`, `session_id`, `replica_id`, `ip_address`, `user_agent` — are
deliberately **fields**: retrievable per point, never a group-by. `email` is a
function of `api_key`, so it adds no series cardinality. Field *types* must stay
consistent per field name (byte counts `int`, durations `float`) — InfluxDB
rejects a point conflicting with an existing series, hence the explicit casts.

| Metric class | Measurement | Tags | Fields | Emitted by |
|---|---|---|---|---|
| `ModelLoadTimeMetric` (`metrics.py:61`) | `model_load_time` | `model_key`, `load_type` (`initial` \| `from_cache`) | `duration_ms`, `num_gpus` | `modeling/base.py:177` (disk load), `modeling/base.py:237` (WARM→HOT restore) |
| `GPUMemMetric` (`:85`) | `gpu_mem` | `model_key`, `api_key`, `email`, `gpu_index` | `request_id`, `baseline_bytes`, `peak_bytes`, `extra_bytes` | `modeling/base.py:333` — one point per device, from `gpu_baselines`/`gpu_peaks` |
| `RequestSizeMetric` (`:126`) | `request_size` | `model_key`, `api_key`, `email` | `request_id`, `session_id`, `ip_address`, `user_agent`, `payload_bytes` | `services/api/app.py:188`, at ingress |
| `RequestResponseSizeMetric` (`:163`) | `response_size` | `model_key`, `api_key`, `email` | `request_id`, `response_bytes`, `compressed` | `modeling/base.py:552`, in `upload_bytes` |
| `ExecutionTimeMetric` (`:189`) | `execution_time` | `model_key`, `api_key`, `email`, `status` (`completed` \| `error` \| `timeout` \| `cancelled`) | `request_id`, `replica_id`, `exec_ms`, `deserialize_ms`, `upload_ms` | `modeling/base.py:494`, in `BaseModelDeployment.report` |
| `RequestStatusTimeMetric` (`:241`) | `status_time` | `model_key`, `api_key`, `email`, `status` | `request_id`, `duration_ms` | `common/schema/request.py:112` (every transition), `services/api/app.py:163` (the synthetic `SENT` hop) |

Notes on the two that need them:

- **`execution_time` splits the run into phases** — `deserialize_ms`
  (rehydrating the block), `exec_ms` (the user's interventions), `upload_ms`
  (staging the result blob) — so a slow request is attributable. Any field may
  be absent (a request that errored before deserialize finished has no
  `deserialize_ms`) and absent fields are simply dropped. `replica_id` is
  declared but `report()` never passes it, so today it is always dropped.
- **`status_time` is the whole latency breakdown, recorded centrally.** Each
  transition emits one point for the status being *left*, tagged with it, so a
  request walking `SENT → RECEIVED → QUEUED → DISPATCHED → RUNNING → COMPLETED`
  produces one point per hop with no per-call-site instrumentation: `SENT` is
  client→server transit (from the `ndif-timestamp` header, skipped when it looks
  like clock skew — `app.py:162`), `QUEUED` is queue wait, `RUNNING` is execution.

### Adding a new metric

1. Add a `Metric` subclass in `src/ndif/common/metrics.py`. Set `measurement` to
   a new snake_case name, and write a docstring saying what it measures and why
   the tag/field split is what it is.

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

2. Keep every argument keyword-only, default optional context to `None` (the
   provider drops `None` tags and fields, `influx.py:182`/`:196`), and cast
   field values to a fixed type.
3. Put anything unbounded — ids, URLs, user agents — in `fields`, never `tags`.
4. Emit from the call site, timing with `elapsed_ms(started_at)` from
   `ndif.common.telemetry`.
5. Make sure the emitting process actually connected `InfluxProvider` (see
   below) — a Ray actor that never imported it silently records nothing.
6. Add a row to the table above and to the observability page.

## Fail-open behavior

Nothing here can take down a request.

| Condition | Result |
|---|---|
| `NDIF_LOKI_URL` unset | Loki never imported, console-only — `connect` returns right after `configure_console` (`loki.py:179`). |
| `python-logging-loki` missing, or the handler fails to build | WARNING logged, console-only (`loki.py:185`, `:192`). |
| Loki unreachable at runtime | Records dropped silently — bounded queue drops when full, `handleError` neutered (`loki.py:103`, `:124`). |
| `influxdb-client` missing, or `NDIF_INFLUX_ENABLED` falsey | `connect()` returns immediately and every `write` is a no-op (`influx.py:45`, `:112`). |
| Influx unreachable at connect | Swallowed, logged at DEBUG; metrics stay disabled (`influx.py:137`). |
| Influx unreachable at flush | Batch dropped; `error_callback` logs at DEBUG only, and `max_retries=0` prevents an unbounded buffer or a blocking shutdown (`influx.py:131`, `:156`). |
| Point has no non-`None` fields | Skipped — Influx rejects fieldless points (`influx.py:182`). |

`InfluxProvider.write` never awaits network I/O: the point is enqueued onto the
client's in-memory batch buffer and flushed from a background thread, so it is
safe to call from an asyncio event loop and from the actor's execution thread
alike. Each point gets an explicit `time.time_ns()` timestamp (`influx.py:194`) —
without one, every point in a batch shares the server's ingestion time and
same-tag-set points silently overwrite each other.

## Process wiring

Both providers connect at *import* (`loki.py:232`, `influx.py:225`) and both own a
background thread, which makes the import point load-bearing.

### The API (gunicorn)

`src/ndif/services/api/gunicorn_conf.py` is built around one rule: **the master
must not import the providers before it forks.**

- The config module imports neither the providers nor the dispatcher at module
  level (the dispatcher would pull the providers in transitively).
- `post_fork(server, worker)` (`gunicorn_conf.py:35`) imports
  `ndif.common.providers.influx` and `.loki` **in each worker**, after the fork
  and before the worker loads the app — so every worker gets its own live
  shipper threads instead of a dead inherited one.
- `on_starting` (`:61`) launches the queue dispatcher with a **spawn** context
  (`:64`), and its target `_run_dispatcher` (`:46`) imports everything lazily
  inside the child. A fresh interpreter, so nothing provider-related is ever
  imported into the master.

This assumes gunicorn's default `preload_app = False`; turning preload on would
import the app — and its providers — into the master before the fork and
reintroduce the dead-thread problem. Run standalone (`python -m
ndif.services.api.queue.dispatcher`), the dispatcher does the same imports itself
at `dispatcher.py:393`.

### Ray

`services/ray/start.sh:17` exports `NDIF_SERVICE=ray` before `ray start`, so the
raylet and every actor process it spawns inherit it. Each Ray actor is its own
process and must connect in its own `__init__`: the controller connects **Loki
only** (`controller.py:61`) since it emits events but no metrics, while
`BaseModelDeployment.__init__` connects **both** (`modeling/base.py:117`).

Ray workers only inherit the node's ambient environment, so the controller
propagates its own provider config into each actor's `runtime_env`
(`_provider_runtime_env`, `cluster/deployment.py:16`) and then overrides
`NDIF_SERVICE` to `"model"` (`cluster/deployment.py:187`) so a model actor's
logs and metrics attribute to the model service rather than to the controller.
The controller's launcher does the same for the controller actor
(`controller.py:566`).

## Configuration

| Var | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_LOG_LEVEL` | `INFO` | `logging_setup.py:127` | Level of the `ndif` logger and its console handler. |
| `NDIF_SERVICE` | `unknown` | `loki.py:142`, `influx.py:74` | `service` label/tag on every log and point. Set to `api` / `ray` by the start scripts, `model` for model actors. |
| `NDIF_ENVIRONMENT` | `dev` | `loki.py:143`, `influx.py:75` | `environment` label/tag (prod / staging / dev). |
| `NDIF_LOKI_URL` | `""` | `loki.py:139` | Empty disables Loki entirely. Set to e.g. `http://loki:3100/loki/api/v1/push`. |
| `NDIF_LOKI_LEVEL` | `INFO` | `loki.py:145` | Minimum level shipped to Loki (console keeps its own). |
| `NDIF_LOKI_QUEUE_MAX` | `10000` | `loki.py:147` | In-memory queue bound; records dropped when full. |
| `NDIF_INFLUX_ENABLED` | `True` | `influx.py:70` | `1/true/yes/on` are truthy; anything else disables metrics. |
| `NDIF_INFLUX_URL` | `http://localhost:8086` | `influx.py:66` | InfluxDB 2.x endpoint. |
| `NDIF_INFLUX_TOKEN` / `_ORG` / `_BUCKET` | `""` / `ndif` / `metrics` | `influx.py:67`–`:69` | Credentials and destination for every point. |
| `NDIF_INFLUX_BATCH_SIZE` / `_FLUSH_INTERVAL_MS` | `500` / `1000` | `influx.py:78`–`:79` | Flush when either threshold is hit. |
| `NDIF_INFLUX_TIMEOUT_MS` | `10000` | `influx.py:80` | Per-HTTP-write timeout. |

## Gotchas

> **`NDIF_SERVICE` means two different things.** It is the telemetry
> `service` tag *and* the CLI's service selector — `ndif start` reads it as the
> space/comma-separated list of services to run (`cli/service.py:85`). The
> compose file sets it per container to a single service name, which satisfies
> both readings; setting it to a list would also relabel that container's
> telemetry with the whole list.

> **A process that never imports the providers has no console formatting and no
> telemetry.** `configure_console` is only called from `LokiProvider.connect`. A
> new entry point (a script, a Ray actor) must import
> `ndif.common.providers.loki` — and `.influx` if it emits metrics — or its
> `event()` calls go nowhere useful.

> **A metric's field types are permanent per series.** Writing `duration_ms` as
> an `int` in one place and a `float` in another makes InfluxDB reject the
> conflicting points. Cast in `update()`.

## Related

- [observability.md](../operating/observability.md) — running Loki/Influx/Grafana and what to look at
- [providers.md](./providers.md) — the provider pattern these two sinks follow
- [schemas.md](../reference/schemas.md) — `_advance_status`, the source of the `status_time` metric and lifecycle events
- [env-vars.md](../reference/env-vars.md) — every `NDIF_*` variable in one table
- [trace-a-users-failed-job.md](../runbooks/trace-a-users-failed-job.md) — using `request_id` / `email` across logs and metrics
