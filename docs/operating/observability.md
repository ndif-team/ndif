---
title: Observability
one_liner: Running NDIF's telemetry side — Loki for logs, InfluxDB for NDIF's own metrics, Prometheus for Ray's, Grafana as the viewer, and what to look at for the four questions you actually ask.
tags: [operating, telemetry, runbook, gotchas]
related: [docs/developing/telemetry-internals.md, docs/operating/compose-stack.md, docs/operating/dashboard.md, docs/operating/troubleshooting.md, docs/operating/production.md, docs/developing/providers.md, docs/reference/env-vars.md, docs/reference/ports.md, docs/runbooks/trace-a-users-failed-job.md, docs/runbooks/debug-a-stuck-request.md]
sources: [docker/docker-compose.yml, docker/prometheus/prometheus.yml, docker/grafana/provisioning/datasources/loki.yml, docker/grafana/provisioning/datasources/influx.yml, docker/grafana/provisioning/datasources/prometheus.yml, docker/grafana/provisioning/datasources/postgres.yml, docker/grafana/provisioning/dashboards/dashboards.yml, src/ndif/common/logging_setup.py, src/ndif/common/telemetry.py, src/ndif/common/metrics.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/influx.py, src/ndif/services/ray/start.sh, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/dashboard/jobs/monitor.py, pyproject.toml]
---

# Observability

## What this covers

The four telemetry services in the compose stack, what each one holds, how to
reach it, and which one answers which question. Two design constraints run
through all of it:

- **Everything is fail-open.** Loki or InfluxDB being down, unconfigured, or not
  even installed must never break a request. Both providers degrade to
  console-only and the stack keeps serving.
- **Nothing NDIF writes is Prometheus-shaped.** NDIF ships logs to Loki and
  metrics to InfluxDB. Prometheus exists solely to scrape *Ray's* own metrics.
  Grafana is where the three meet.

The metric-by-metric field reference lives in
`docs/developing/telemetry-internals.md`; this page is the operator's view.

## The four surfaces

| Surface | Holds | Written by | Dev URL |
|---|---|---|---|
| **Loki** | Every `ndif` log record, as a structured JSON line | `LokiProvider` handler on the `ndif` logger | `http://localhost:3100` |
| **InfluxDB** | NDIF's own numeric time series (six measurements) | `ndif.common.metrics` via `InfluxProvider` | `http://localhost:8086` |
| **Prometheus** | Ray's cluster/node/actor metrics | scraped from `ray:8080/metrics` | `http://localhost:9090` |
| **Grafana** | Nothing — it queries the other three (plus Postgres) | — | `http://localhost:3000` |

Grafana is the one you open. It runs with anonymous admin auth in the dev stack
(`GF_AUTH_ANONYMOUS_ENABLED`, `GF_AUTH_DISABLE_LOGIN_FORM`,
`docker/docker-compose.yml:75`) and lands on the **NDIF — Overview** dashboard
(`GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH`, `:85`). See
`docs/reference/ports.md` for every port the stack binds.

> **Gotcha:** none of loki, influxdb, prometheus, or grafana declares a volume in
> `docker/docker-compose.yml` — only the dashboard does. `just down` destroys
> every log, metric, and UI-edited panel. Add volumes before you rely on any of
> it (`docs/operating/production.md`).

## Logs — Loki

`LokiProvider.connect()` (`src/ndif/common/providers/loki.py:159`) attaches a
`LokiQueueHandler` to the `ndif` logger when `NDIF_LOKI_URL` is set, and always
installs the readable console handler regardless. Records are formatted as a
single JSON object; a bounded queue and a background shipper thread mean `emit`
never blocks the event loop.

**Stream labels** (what you put in `{}` in LogQL) are deliberately few, because
label cardinality is what kills Loki:

| Label | Source | Values |
|---|---|---|
| `service` | `NDIF_SERVICE` env | `api`, `ray`, `dashboard`, **`model`** |
| `environment` | `NDIF_ENVIRONMENT`, default `dev` | your choice |
| `logger` | added by `logging_loki` | the `ndif.*` sub-logger |
| `severity` | added by `logging_loki` | `info`, `warning`, `error`, … |
| `model_key` | promoted per record by `_LabelFilter` (`loki.py:71`) | bounded by served models |

> **Gotcha: there are four `service` values, but only three appear in any config
> file.** `NDIF_SERVICE` does double duty — it selects which service `ndif start`
> runs *and* labels that process's telemetry — so `api`, `ray` and `dashboard`
> are visible in `docker/docker-compose.yml`. **`model` is not.** The controller
> injects `"NDIF_SERVICE": "model"` into each model actor's Ray `runtime_env`
> (`cluster/deployment.py:187`) precisely so a replica's logs and metrics
> attribute to the model rather than to the controller that spawned it. A replica
> is a separate process on the ray node, and its logs are **not** under
> `{service="ray"}` — that label carries only the Ray node's own output
> (`ndif.controller`, raylet). Chasing a request through execution means
> `{service="model"}`, or dropping the label entirely.

**Everything else is in the JSON line**, queryable with `| json` but not indexed:
`message`, `host`, `pid`, `thread`, `func`, `file`, `line` (`logging_setup.py:49`),
plus every `extra=` field, plus `exception_type` / `exception_message` /
`stacktrace` when the record carries an exception (`:61`). `request_id` and
`session_id` live here on purpose — they're unbounded.

Logger names, all children of `ndif`:

| Logger | `service` | Emits |
|---|---|---|
| `ndif.api` | `api` | HTTP ingress: rejections with `status_code`/`path`, unhandled 500s |
| `ndif.request` | `api` **and** `model` | The lifecycle event on every status change, with `stage`, `prev_stage`, `prev_stage_ms` — `respond()` is called from both sides |
| `ndif.queue.dispatcher` | `api` | Redis/Ray connection state, status workers |
| `ndif.queue.processor` | `api` | Enqueue, provisioning, autoscaling |
| `ndif.queue.replica` | `api` | Dispatch to an actor, replica loss |
| `ndif.controller` | `ray` | Node updates, placement decisions, evictions |
| `ndif.modeling` | `model` | Model load, execution errors, timeouts, cancellations |
| `ndif.dashboard.reconcile` | `dashboard` | The schedule reconcile cron |

Useful LogQL:

```logql
{service="api"} | json | stage=`received`          # ingress rate
{severity="error"} | json                          # every error, all services
{service="model"} | json | request_id=`<id>`       # one job on the model side
{environment=~".+"} | json | request_id=`<id>`     # one job end to end
{logger="ndif.controller"} |= "Evicting"           # eviction history
{service="api"} | json | status_code >= 500        # server-side rejections
{service="model"} | json | model_key=`<key>`       # one model's replicas
```

When in doubt, filter on `logger` rather than `service` — `{logger="ndif.modeling"}`
finds model-actor logs whatever the process is labelled, and `{environment=~".+"}`
matches everything without committing to a service at all.

Grafana's Loki datasource adds **derived fields**
(`docker/grafana/provisioning/datasources/loki.yml:22`): `request_id`, `email`,
and `host` in any log line render as links — `request_id` to every log for that
request across all services, `email` to that user's dashboard, `host` to
everything from that process.

## Metrics — InfluxDB

`ndif.common.metrics` defines one class per measurement; each writes through
`InfluxProvider.write` (`providers/influx.py:165`), which merges the process base
tags (`service`, `environment`), stamps an explicit nanosecond timestamp, and
enqueues onto a batching writer that flushes from a background thread.

| Measurement | Records | Key fields |
|---|---|---|
| `request_size` | Incoming payload size at ingress | `payload_bytes`, `ip_address`, `user_agent` |
| `response_size` | Result blob staged in the object store | `response_bytes`, `compressed` |
| `execution_time` | Time on the model actor, split by phase | `deserialize_ms`, `exec_ms`, `upload_ms` |
| `status_time` | Time spent in each lifecycle status | `duration_ms`, tagged by the status it *left* |
| `gpu_mem` | Extra GPU a request drove past the resident weights | `baseline_bytes`, `peak_bytes`, `extra_bytes` |
| `model_load_time` | Weights onto GPU | `duration_ms`, `num_gpus`, tagged `load_type` |

The tag/field split is the load-bearing convention: **tags** are `service`,
`environment`, `model_key`, `api_key`, `email`, and small enumerations
(`status`, `load_type`, `gpu_index`) — group by these. **Fields** carry the
values plus unbounded context (`request_id`, `session_id`, `replica_id`,
`ip_address`) — retrievable per point, never a group-by dimension. Field types
are fixed per name because InfluxDB rejects a type conflict on an existing
series. Full per-metric detail is in `docs/developing/telemetry-internals.md`.

`status_time` is the one people miss. Every status transition emits a point for
the phase just *left* (`common/schema/request.py:112`), so a request walking
`SENT → received → queued → dispatched → running → completed` produces one point
per hop and end-to-end latency is attributable without extra instrumentation.

## Ray metrics — Prometheus

Ray's per-node metrics agent exports Prometheus format on the
**`--metrics-export-port`**, which `start.sh` sets from `NDIF_RAY_METRICS_PORT`
(default `8080`):

```bash
ray start --head \
    ...
    --metrics-export-port="${NDIF_RAY_METRICS_PORT:-8080}" \
```

(`src/ndif/services/ray/start.sh:66`.) That is exactly the target Prometheus
scrapes — `targets: ["ray:8080"]` at a 10s interval
(`docker/prometheus/prometheus.yml:20`), reached by service name on the compose
network, not published to the host. NDIF does not use Ray Serve anywhere.

Retention is `--storage.tsdb.retention.time=15d`
(`docker/docker-compose.yml:63`). Prometheus has no `depends_on: ray` and simply
retries until the (slow, GPU-bound) ray container is up.

Useful series: `ray_node_cpu_utilization`, `ray_node_gpus_utilization`,
`ray_node_mem_used`, `ray_actors` (by state), `ray_tasks` (by state),
`ray_object_store_memory`. The **NDIF — Ray Cluster** dashboard charts all of
these.

> For a real multi-node cluster, replace `static_configs` with `file_sd_configs`
> pointing at Ray's `<temp_dir>/prom_metrics_service_discovery.json`, which Ray
> rewrites as nodes join and leave (`docker/prometheus/prometheus.yml:8`).

## Grafana — the viewer

Four datasources are provisioned from
`docker/grafana/provisioning/datasources/`: **Loki** (default), **InfluxDB**
(Flux, org `ndif`, bucket `metrics`), **Prometheus**, and **Postgres** — the last
so dashboards can resolve an `api_key` to a user email against the same
read-only `ndifapi` account the API uses.

Nine dashboards are provisioned into an **NDIF** folder
(`dashboards/dashboards.yml`), rescanned every 30s:

| Dashboard | Source | Use it for |
|---|---|---|
| **NDIF — Overview** (home) | Loki + Influx + Prom | Request rate, error rate, exec p95, active users, Ray CPU/GPU |
| NDIF — Requests & Throughput | Loki | Rate by model, lifecycle stage rates, queue depth, top users |
| NDIF — Errors & Health | Loki | Error/warning counts, errors by type and by user, API rejections by status code, recent error logs |
| NDIF — Latency & Performance | Influx | Exec p50/p95, deserialize/exec/upload split, outcomes by status |
| NDIF — Latency Distributions | Influx | Heatmaps and histograms of total/queued/exec time |
| NDIF — GPU & Model Load | Influx | Extra GPU bytes per request by model and device, load p95 |
| NDIF — Sizes & Throughput | Influx | Request/response size p95, data volume in/out, top users by bytes |
| NDIF — Users & Usage | Postgres + Influx | Per-user (by email) requests, bytes, exec p95, outcomes |
| NDIF — Ray Cluster | Prometheus | Nodes, CPU/GPU utilization, actors and tasks by state, object store |

## What to look at

**Is the cluster healthy?** Overview → *Ray CPU / GPU utilization* and *Error
rate*, then Ray Cluster → *Nodes* and *Actors by state*. A node that vanished
shows up there before anywhere else. Cross-check `ndif status` for what the
controller *thinks* it has — the controller's placement bookkeeping is
independent of Ray's own accounting, and a disagreement is the interesting
signal.

**Why is this request slow?** Latency & Performance → *Lifecycle phases* tells
you which phase. `status_time` tagged `queued` means it waited for a replica
(check queue depth on Requests & Throughput); `status_time` tagged `running`, or
`execution_time.exec_ms`, means the user's code is slow. A large
`deserialize_ms` means a big serialized block; a large `upload_ms` means a big
result blob (see Sizes & Throughput).

**Which model is eating GPU?** GPU & Model Load → *GPU extra bytes p95 by model*
for per-request footprint, and `ndif status` for resident weights. These measure
different things: `gpu_mem.extra_bytes` is the activation/KV footprint a single
request drove *on top of* the weights, not the weights themselves.

**Did this user's job fail?** Errors & Health → *Errors by user*, or go straight
to Loki with the cross-service request query above: it returns the API's ingress
record, every lifecycle transition, and the model actor's traceback in one
stream. If you only have an email, the Users & Usage dashboard
(`/d/ndif-users/?var-email=...`) narrows it down first. See
`docs/runbooks/trace-a-users-failed-job.md`.

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `NDIF_LOKI_URL` | *(empty)* | **Empty disables Loki entirely** — the package is never imported |
| `NDIF_LOKI_LEVEL` | `INFO` | Minimum level shipped (console keeps its own) |
| `NDIF_LOKI_QUEUE_MAX` | `10000` | In-memory queue bound; records drop when full |
| `NDIF_LOG_LEVEL` | `INFO` | Console handler level |
| `NDIF_INFLUX_URL` | `http://localhost:8086` | InfluxDB 2.x endpoint |
| `NDIF_INFLUX_TOKEN` / `_ORG` / `_BUCKET` | *(empty)* / `ndif` / `metrics` | Auth and destination |
| `NDIF_INFLUX_ENABLED` | `true` | Hard off switch |
| `NDIF_INFLUX_BATCH_SIZE` | `500` | Flush at N points… |
| `NDIF_INFLUX_FLUSH_INTERVAL_MS` | `1000` | …or after this long |
| `NDIF_INFLUX_TIMEOUT_MS` | `10000` | Per-write HTTP timeout |
| `NDIF_SERVICE` | `unknown` | Label/tag on everything this process emits |
| `NDIF_ENVIRONMENT` | `dev` | Label/tag distinguishing prod / staging / dev |

Full table in `docs/reference/env-vars.md`. Retention: Prometheus 15d (compose
flag); Loki and InfluxDB run their images' defaults with no retention policy set
here, and no volumes, so their real "retention" is the container's lifetime.

## Fail-open

Both telemetry providers are optional in three independent ways:

1. **Not installed.** `influxdb-client` and `python-logging-loki` are in the
   `metrics` extra (`pyproject.toml:62`), not the core deps. Without it,
   `InfluxProvider` never constructs a client (`_HAS_CLIENT = False`,
   `influx.py:55`) and `LokiProvider` catches the `ImportError` and warns once
   (`loki.py:185`). Every write is a no-op.
2. **Not configured.** An empty `NDIF_LOKI_URL` short-circuits before
   `logging_loki` is even imported (`loki.py:179`); `NDIF_INFLUX_ENABLED=false`
   does the same for metrics.
3. **Down.** The Loki handler drops on a full queue rather than raising, and its
   inner `handleError` is silenced so an outage doesn't flood stderr with one
   traceback per record (`loki.py:122`). Influx writes with `max_retries=0` and
   logs a failed batch at DEBUG (`influx.py:131`).

In all three cases the readable console handler is still installed — structured
output on stdout is worth having on its own, so `docker compose logs api` never
goes quiet.

> **Gotcha:** both providers own a background thread, and threads don't survive
> `fork()`. The gunicorn master avoids importing them until after it forks; each
> worker connects in `post_fork` (`api/gunicorn_conf.py:35`). Ray actors are
> separate processes, so the controller and every model actor call
> `LokiProvider.connect()` / `InfluxProvider.connect()` in their own `__init__`.
> If you add a new entry point, connect *after* forking.

## The fifth source: the dashboard monitor cron

The admin dashboard runs a monitor cron (default `*/10 * * * *`) that is
NDIF-specific uptime data no Grafana panel produces: it probes `/connected` and
`/status`, and every `--model-interval` seconds (default 2h) runs a real remote
nnsight trace against every HOT model
(`src/ndif/services/dashboard/jobs/monitor.py`). Results land in rotating
`connected_*.log` / `cluster_*.log` / `models_*.log` files under the dashboard's
data dir and drive its uptime charts; down/up transitions and newly-failing model
sets fire Discord notifications.

The distinction that matters: Grafana tells you what *happened to traffic*; the
monitor tells you whether an *end-to-end trace still works* on a cluster with no
traffic at all. See `docs/operating/dashboard.md`.

## Related

`docs/developing/telemetry-internals.md` is the per-metric and per-field
reference and the code behind `event()`; `docs/developing/providers.md` covers
the provider pattern these two sinks share. For where each container gets its
telemetry env, `docs/operating/compose-stack.md`; for the variables themselves,
`docs/reference/env-vars.md`; for the ports, `docs/reference/ports.md`.
`docs/operating/dashboard.md` covers the monitor and reconcile crons, and
`docs/runbooks/trace-a-users-failed-job.md` and
`docs/runbooks/debug-a-stuck-request.md` put this page to work.
