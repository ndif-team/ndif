---
title: Observability
one_liner: Running NDIF's telemetry side — Loki for logs, InfluxDB for NDIF's own metrics, Prometheus for Ray's, Grafana as the viewer, and what to look at for the four questions you actually ask.
tags: [operating, telemetry, runbook, gotchas]
related: [docs/developing/telemetry-internals.md, docs/operating/compose-stack.md, docs/operating/dashboard.md, docs/operating/troubleshooting.md, docs/operating/production.md, docs/developing/providers.md, docs/reference/env-vars.md, docs/reference/ports.md, docs/runbooks/trace-a-users-failed-job.md, docs/runbooks/debug-a-stuck-request.md]
sources: [docker/docker-compose.yml, docker/prometheus/prometheus.yml, docker/grafana/provisioning/datasources/loki.yml, docker/grafana/provisioning/datasources/influx.yml, docker/grafana/provisioning/datasources/prometheus.yml, docker/grafana/provisioning/datasources/postgres.yml, docker/grafana/provisioning/dashboards/dashboards.yml, src/ndif/common/logging_setup.py, src/ndif/common/telemetry.py, src/ndif/common/metrics.py, src/ndif/common/schema/request.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/influx.py, src/ndif/services/ray/start.sh, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/api/gunicorn_conf.py, pyproject.toml]
---

# Observability

## What this covers

The four telemetry services in the compose stack, what each one holds, how to
reach it, and which one answers which question. Two design constraints run
through all of it:

- **Everything is fail-open.** Loki or InfluxDB being down, unconfigured, or not
  even installed never breaks a request. Both providers degrade to console-only
  and the stack keeps serving.
- **Nothing NDIF writes is Prometheus-shaped.** NDIF ships logs to Loki and
  metrics to InfluxDB. Prometheus exists solely to scrape *Ray's* own metrics.
  Grafana is where the three meet.

The metric-by-metric field reference lives in
[Telemetry Internals](../developing/telemetry-internals.md); this page is the
operator's view.

## The four surfaces

| Surface | Holds | Written by | Dev URL |
|---|---|---|---|
| **Loki** | Every `ndif` log record, as a structured JSON line | `LokiProvider` handler on the `ndif` logger | `http://localhost:3100` |
| **InfluxDB** | NDIF's own numeric time series (six measurements) | `ndif.common.metrics` via `InfluxProvider` | `http://localhost:8086` |
| **Prometheus** | Ray's cluster/node/actor metrics | scraped from `ray:8080/metrics` | `http://localhost:9090` |
| **Grafana** | Nothing — it queries the other three (plus Postgres) | — | `http://localhost:3000` |

Grafana is the one you open. In the dev stack it runs with anonymous admin and
the login form disabled (the `grafana` service's `GF_AUTH_*` settings) and lands
on the **NDIF — Overview** dashboard.

None of loki, influxdb, prometheus or grafana has a volume in the compose file,
so `just down` destroys every log, metric and UI-edited panel. Add volumes
before you rely on any of it ([Going to Production](production.md)).

## Logs — Loki

`LokiProvider.connect` (`src/ndif/common/providers/loki.py`) attaches a
`LokiQueueHandler` to the `ndif` logger when `NDIF_LOKI_URL` is set, and always
installs the readable console handler. Records are formatted as a single JSON
object; a bounded queue and a background shipper thread mean `emit` never
blocks the event loop.

**Stream labels** (what you put in `{}` in LogQL) are deliberately few, because
label cardinality is what kills Loki:

| Label | Source | Values |
|---|---|---|
| `service` | `NDIF_SERVICE` (default `unknown`) | `api`, `ray`, `dashboard`, **`model`** |
| `environment` | `NDIF_ENVIRONMENT`, default `dev` | your choice |
| `logger` | added by `logging_loki` | the `ndif.*` sub-logger |
| `severity` | added by `logging_loki` | `info`, `warning`, `error`, … |
| `model_key` | promoted per record by `_LabelFilter` | bounded by served models |

**Model-actor logs are under `service="model"`, not `service="ray"`.** The
controller injects `NDIF_SERVICE=model` into each model actor's Ray
`runtime_env`, so a replica's logs and metrics attribute to the model rather
than to the controller that spawned it. `{service="ray"}` carries only the Ray
node's own output (`ndif.controller`, raylet). Chasing a request through
execution means `{service="model"}`, or dropping the label entirely.

**Everything else is in the JSON line**, queryable with `| json` but not
indexed: `message`, the `source_context` fields (`host`, `pid`, `thread`,
`func`, `file`, `line`), every `extra=` field, and `exception_type` /
`exception_message` / `stacktrace` when the record carries an exception
(`src/ndif/common/logging_setup.py`). `request_id` and `session_id` live here on
purpose — they're unbounded.

| Logger | `service` | Emits |
|---|---|---|
| `ndif.api` | `api` | HTTP ingress: rejections with `status_code`/`path`, unhandled 500s |
| `ndif.request` | `api` **and** `model` | The lifecycle event on every status change, with `stage`, `prev_stage`, `prev_stage_ms` |
| `ndif.queue.dispatcher` | `api` | Redis/Ray connection state, status workers |
| `ndif.queue.processor` | `api` | Enqueue, provisioning, autoscaling |
| `ndif.queue.replica` | `api` | Dispatch to an actor, replica loss |
| `ndif.controller` | `ray` | Node updates, placement decisions, evictions |
| `ndif.modeling` | `model` | Model load, execution errors, timeouts, preemptions |
| `ndif.dashboard.reconcile` | `dashboard` | The schedule reconcile cron |

```logql
{service="api"} | json | stage=`received`          # ingress rate
{severity="error"} | json                          # every error, all services
{environment=~".+"} | json | request_id=`<id>`     # one job end to end
{logger="ndif.controller"} |= "Evicting"           # eviction history
{service="api"} | json | status_code >= 500        # server-side rejections
{service="model"} | json | model_key=`<key>`       # one model's replicas
```

When in doubt, filter on `logger` rather than `service`, and use
`{environment=~".+"}` to match everything without committing to a service.
Grafana's Loki datasource adds **derived fields**
(`docker/grafana/provisioning/datasources/loki.yml`): `request_id`, `email` and
`host` in any log line render as links to every log for that request, that
user's dashboard, and that process.

## Metrics — InfluxDB

`ndif.common.metrics` defines one class per measurement; each writes through
`InfluxProvider.write` (`src/ndif/common/providers/influx.py`), which merges the
process base tags (`service`, `environment`), stamps a nanosecond timestamp, and
enqueues onto a batching writer that flushes from a background thread.

| Measurement | Records | Key fields |
|---|---|---|
| `request_size` | Incoming payload size at ingress | `payload_bytes`, `ip_address`, `user_agent` |
| `response_size` | Result blob staged in the object store | `response_bytes`, `compressed` |
| `execution_time` | Time on the model actor, split by phase, tagged with the outcome | `deserialize_ms`, `exec_ms`, `upload_ms` |
| `status_time` | Time spent in each lifecycle status | `duration_ms`, tagged by the status it *left* |
| `gpu_mem` | Extra GPU a request drove past the resident weights | `baseline_bytes`, `peak_bytes`, `extra_bytes` |
| `model_load_time` | Weights onto GPU | `duration_ms`, `num_gpus`, tagged `load_type` |

**Tags** are `service`, `environment`, `model_key`, `api_key`, `email`, and small
enumerations (`status`, `load_type`, `gpu_index`) — group by these. **Fields**
carry the values plus unbounded context (`request_id`, `session_id`,
`replica_id`, `ip_address`) — retrievable per point, never a group-by dimension.
Field types are fixed per name, because InfluxDB rejects a type conflict on an
existing series.

**`status_time`** is the one people miss. Every status transition emits a point
for the phase just *left* (`src/ndif/common/schema/request.py`
`BackendRequestModel._advance_status`), so end-to-end latency is attributable
hop by hop without extra instrumentation.

**`execution_time`'s `status` tag** is one of `completed`, `error`, `timeout`,
`cancelled` or `preempted` (`BaseModelDeployment.report` in
`src/ndif/services/ray/deployments/modeling/base.py`). `preempted` means the
replica was parked mid-run and the request went back on the queue, so that
request has a second point for its re-run. A request stopped with `ndif kill`
is marked `cancelled`, unless the kill fell back to cancelling the dispatch (the
actor had not started it), in which case the actor finishes the block and the
point carries the block's own outcome. Count user-visible failures from
the `ndif.request` lifecycle events (`stage` = `error`), not from this tag.

## Ray metrics — Prometheus

Ray's per-node metrics agent exports Prometheus format on the
`--metrics-export-port`, which `src/ndif/services/ray/start.sh` sets from
`NDIF_RAY_METRICS_PORT` (default `8080`). That is the one target Prometheus
scrapes — `ray:8080` at a 10s interval (`docker/prometheus/prometheus.yml`),
reached by service name on the compose network, not published to the host.
Retention is the compose flag `--storage.tsdb.retention.time=15d`.

Useful series: `ray_node_cpu_utilization`, `ray_node_gpus_utilization`,
`ray_node_mem_used`, `ray_actors` (by state), `ray_tasks` (by state),
`ray_object_store_memory`. The **NDIF — Ray Cluster** dashboard charts all of
them. For a multi-node cluster, replace `static_configs` with `file_sd_configs`
pointing at Ray's `<temp_dir>/prom_metrics_service_discovery.json`, which Ray
rewrites as nodes join and leave.

## Grafana — the viewer

Four datasources are provisioned from `docker/grafana/provisioning/datasources/`:
**Loki** (default), **InfluxDB** (Flux, org `ndif`, bucket `metrics`),
**Prometheus**, and **Postgres** — the last so dashboards can resolve an
`api_key` to a user email through the same read-only `ndifapi` account the API
uses. Nine dashboards are provisioned into an **NDIF** folder, rescanned every
30s:

| Dashboard | Source | Use it for |
|---|---|---|
| **NDIF — Overview** (home) | Loki + Influx + Prom | Request rate, error rate, exec p95, active users, Ray CPU/GPU |
| NDIF — Requests & Throughput | Loki | Rate by model, lifecycle stage rates, queue depth, top users |
| NDIF — Errors & Health | Loki | Errors by type and by user, API rejections by status code, recent error logs |
| NDIF — Latency & Performance | Influx | Exec p50/p95, deserialize/exec/upload split, outcomes by status |
| NDIF — Latency Distributions | Influx | Heatmaps and histograms of total/queued/exec time |
| NDIF — GPU & Model Load | Influx | Extra GPU bytes per request by model and device, load p95 |
| NDIF — Sizes & Throughput | Influx | Request/response size p95, data volume in/out, top users by bytes |
| NDIF — Users & Usage | Postgres + Influx | Per-user (by email) requests, bytes, exec p95, outcomes |
| NDIF — Ray Cluster | Prometheus | Nodes, CPU/GPU utilization, actors and tasks by state, object store |

## What to look at

**Is the cluster healthy?** Overview → *Ray CPU / GPU utilization* and *Error
rate*, then Ray Cluster → *Nodes* and *Actors by state*. Cross-check
`ndif status` for what the controller *thinks* it has — its placement
bookkeeping is independent of Ray's own accounting, and a disagreement is the
interesting signal. For whether a trace works end to end on an idle cluster, the
dashboard's monitor cron is the source ([Admin Dashboard](dashboard.md#the-monitor)).

**Why is this request slow?** Latency & Performance → *Lifecycle phases* tells
you which phase. `status_time` tagged `queued` means it waited for a replica
(check queue depth on Requests & Throughput); `status_time` tagged `running`, or
`execution_time.exec_ms`, means the user's code is slow. A large
`deserialize_ms` means a big serialized block; a large `upload_ms`, a big result
blob.

**Which model is eating GPU?** GPU & Model Load → *GPU extra bytes p95 by model*
for per-request footprint, and `ndif status` for resident weights.
`gpu_mem.extra_bytes` is the activation/KV footprint a single request drove *on
top of* the weights, not the weights themselves.

**Did this user's job fail?** Errors & Health → *Errors by user*, or Loki with
the end-to-end request query above: it returns the API's ingress record, every
lifecycle transition, and the model actor's traceback in one stream. If you only
have an email, the Users & Usage dashboard narrows it down first. See
[Trace a user's failed job](../runbooks/trace-a-users-failed-job.md).

## Configuration and fail-open

The variables are in [Env vars — Metrics](../reference/env-vars.md#metrics-influxdb)
and [Logs](../reference/env-vars.md#logs-loki). An empty `NDIF_LOKI_URL` or
`NDIF_INFLUX_URL` turns that sink off.

Neither sink can take a service down: missing, unconfigured or unreachable, it
degrades to a no-op and the console handler keeps writing, so `docker compose
logs api` never goes quiet. Each case, and why a new entry point must connect
after forking, is in
[Telemetry Internals](../developing/telemetry-internals.md#fail-open-behavior).

Loki and InfluxDB run their images' default retention, and with no volumes their
real retention is the container's lifetime.

## Related

[Telemetry Internals](../developing/telemetry-internals.md) is the per-metric and
per-field reference; [Providers](../developing/providers.md) covers the pattern
these two sinks share. For where each container gets its telemetry env,
[The Compose Stack](compose-stack.md); for every variable,
[Environment Variables](../reference/env-vars.md).
[Admin Dashboard](dashboard.md) covers the monitor, reconcile and report crons,
and [Trace a user's failed job](../runbooks/trace-a-users-failed-job.md) and
[Debug a stuck request](../runbooks/debug-a-stuck-request.md) put this page to
work.
