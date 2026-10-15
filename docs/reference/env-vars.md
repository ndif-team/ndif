---
title: Environment Variables
one_liner: Every NDIF_* variable the server reads — default, the symbol that reads it, and what it changes.
tags: [reference, operating, config]
related: [docs/operating/configuration.md, docs/reference/ports.md, docs/operating/compose-stack.md, docs/operating/production.md, docs/developing/providers.md, docs/concepts/auth-and-limits.md, docs/operating/observability.md]
sources: [src/ndif/cli/config.py, src/ndif/common/providers/redis.py, src/ndif/common/providers/ray.py, src/ndif/common/providers/objectstore.py, src/ndif/common/providers/postgres.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/influx.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/api/versioning.py, src/ndif/services/api/queue/config.py, src/ndif/services/ray/start.sh, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/dashboard/start.sh, src/ndif/services/dashboard/backend/config.py, src/ndif/common/redis/env.py, src/ndif/common/redis/status.py, src/ndif/common/logging_setup.py, src/ndif/cli/commands/doctor.py, docker/Dockerfile, docker/docker-compose.yml]
---

# Environment Variables

## What this covers

Every environment variable any NDIF process reads, grouped by subsystem, with the
symbol that reads it. This is the one home for the variable list; other pages link
here rather than repeating it.

## The config model

There is no config file: every knob is an environment variable, most read once at
import, so a change needs a restart of the process that reads it. A variable
missing from a process's environment falls back to that process's hardcoded
default, never to another service's value. Later layers win: code defaults → the
CLI's `DEFAULTS` (`src/ndif/cli/config.py` `DEFAULTS`, laid *beneath* the real
environment by `build_env`) → `.env` files (`load_env_files`) → per-process
overrides (`ndif start -e KEY=VALUE`, the typed shortcuts in `ENV_OPTIONS`, a
compose service's `environment:`). Containers run `ndif start --foreground`, so
the CLI defaults apply inside compose too. The layering, worked through, is in
[configuration.md](../operating/configuration.md).

Config crosses one process boundary by itself: creating a model actor exports the
controller's Redis, object-store, Loki and Influx settings into the actor's Ray
`runtime_env` (`src/ndif/services/ray/deployments/controller/cluster/deployment.py`
`_provider_runtime_env`, applied in `Deployment.create`).

## Identity and logging

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_SERVICE` | `all` (image `ENV` in `docker/Dockerfile`); `unknown` as a telemetry label | `src/ndif/cli/service.py` `env_services`; `LokiProvider.CONFIG`, `InfluxProvider.CONFIG` | Which service(s) a bare `ndif start` brings up (space/comma list; `all` = redis, rustfs, ray, api), **and** the `service` label on every log line and metric point. The start scripts export it per service; `Deployment.create` overrides it to `model` for actor processes. |
| `NDIF_ENVIRONMENT` | `dev` | `LokiProvider.CONFIG`, `InfluxProvider.CONFIG`; the report cron's `parse_args` | Second static label on all telemetry — the prod/staging/dev discriminator. The report cron filters usage by it. |
| `NDIF_LOG_LEVEL` | `INFO` | `src/ndif/common/logging_setup.py` `configure_console` | Level of the `ndif` logger (not the root; `propagate` is off). Applied only if it *lowers* the current level. |
| `NDIF_HOME` | `~/.ndif` | `src/ndif/cli/state.py` `State.from_env`; `src/ndif/cli/config.py` `DEFAULTS` | CLI state root: `run/<name>.pid`, `logs/<name>.log`, and RustFS's data dir when the CLI spawns it. CLI-only. |

## API service

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_API_PORT` | `8001` | `src/ndif/services/api/gunicorn_conf.py` `bind` | Port gunicorn binds, always on `0.0.0.0`. |
| `NDIF_API_WORKERS` | `1` | `src/ndif/services/api/gunicorn_conf.py` `workers` | Uvicorn worker count. The queue dispatcher is started once by the master (`on_starting`), so this scales ingress only. |
| `NDIF_API_TIMEOUT` | `120` | `src/ndif/services/api/gunicorn_conf.py` `timeout` | Gunicorn worker timeout in seconds. |
| `NDIF_API_URL` | `http://localhost:8001` | `src/ndif/cli/config.py` `DEFAULTS` (the CLI's `env`, `info`, `doctor`); `src/ndif/services/dashboard/jobs/report.py` `parse_args` | Where *clients of the API* find it: the CLI's checks, and the dashboard's daily report cron, which reads `GET /status` there to count hot models (`hot_models`). Does not affect what the API binds. |
| `NDIF_MIN_NNSIGHT_VERSION` | unset | `src/ndif/services/api/versioning.py` `MIN_NNSIGHT_VERSION` | Minimum client nnsight version. Unset or empty disables the check; set, an older client gets a 400 at submit. |
| `NDIF_MIN_PYTHON_VERSION` | unset | `src/ndif/services/api/versioning.py` `MIN_PYTHON_VERSION` | Same, for the client's Python version. |
| `NDIF_API_KEY` | unset | `src/ndif/services/dashboard/jobs/monitor.py` `resolve_api_key` | A *client* key, used only by the dashboard's monitor cron so its synthetic traces can authenticate. Nothing in the API or CLI reads it. |

## Queue and autoscaling

Read once at import by `src/ndif/services/api/queue/config.py` `QueueConfig.from_env`,
in the API's dispatcher process. The six numeric ones go through `_positive_int`,
which raises at import on a non-integer or non-positive value — the API won't boot.

| Variable | Default | Effect |
|---|---|---|
| `NDIF_QUEUE_KEY` | `queue` | Redis list the API pushes onto and the dispatcher pops from. |
| `NDIF_QUEUE_FETCH_TIMEOUT_S` | `10` | Blocking-pop timeout; bounds how long an idle dispatch loop waits before re-checking evictions and errors. |
| `NDIF_QUEUE_FETCH_BATCH_MAX` | `32` | Max requests drained per dispatch iteration. |
| `NDIF_AUTOSCALING_INTERVAL_S` | `5` | How often a per-model processor inspects its queue. |
| `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` | `30` | Scale up when the oldest queued request has waited longer than this. |
| `NDIF_AUTOSCALING_BACKOFF_S` | `120` | Pause after a scale-up before another can fire. |
| `NDIF_AUTOSCALING_MAX_REPLICAS` | `3` | Per-model replica ceiling for autoscaling. |

## Redis and the shared caches

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_REDIS_URL` | `redis://localhost:6379` | `src/ndif/common/providers/redis.py` `RedisProvider`; `src/ndif/cli/service.py` `_redis_command` | The one Redis every service shares: request queue, response pub/sub, status/env caches, trigger streams. The CLI starts `redis-server` on this URL's port. The inline-result cap derives from this server's `client-output-buffer-limit pubsub` (`RedisProvider.max_publish_bytes`: `min(soft, hard // 2)`, 8 MiB on stock redis:7); there is deliberately no env var for it, so raise the Redis limits to raise the cap. |
| `NDIF_ENV_TTL_S` | `300` | `src/ndif/common/redis/env.py` `ENV_TTL_S` | How long a cached `/env` payload is served before a refresh is triggered. |
| `NDIF_ENV_TIMEOUT_S` | `60` | `src/ndif/common/redis/env.py` `ENV_TIMEOUT_S` | How long a `/env` request waits for a refresh, and how long the refresh holds the coalescing lock. |
| `NDIF_STATUS_TTL_S` | `60` | `src/ndif/common/redis/status.py` `STATUS_TTL_S` | Same, for the cached `/status` blob. |
| `NDIF_STATUS_TIMEOUT_S` | `60` | `src/ndif/common/redis/status.py` `STATUS_TIMEOUT_S` | Same, for a waiting `/status` request. |

> **Gotcha:** every service other than the one running Redis needs
> `NDIF_REDIS_URL` set explicitly. In compose, Redis is its own container, so
> nothing listens on `localhost:6379` inside the `ray` container; left at the
> default, a model actor cannot publish its response and the request looks hung.
> Compose sets the key on the `api`, `ray` and `dashboard` services.

## Ray node

Read by `src/ndif/services/ray/start.sh` (ray service only), except
`NDIF_RAY_ADDRESS`, read by everything that talks *to* Ray.

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | `src/ndif/common/providers/ray.py` `RayProvider.CONFIG`; `src/ndif/cli/config.py` `DEFAULTS` | The `ray://` **client** address the API's dispatcher, the dashboard and the CLI connect with. Says nothing about head vs worker. A URL with no port logs a warning and falls back to 6379 (`RayProvider.get_host_port`). |
| `NDIF_RAY_HEAD_ADDRESS` | unset | `start.sh`; `src/ndif/cli/commands/start.py` `start` | **The head/worker switch.** Unset → `ray start --head`. Set to the head's `HOST:PORT` → wait for it, then join as a worker (a `ray://` prefix is stripped). When set, a bare `ndif start` brings up only Ray. |
| `NDIF_RAY_HEAD_PORT` | `6385` | `start.sh`; `src/ndif/cli/config.py` `DEFAULTS` | Ray's GCS port on the head (`--port`). Both defaults are 6385, off Redis's 6379. Head only. |
| `NDIF_RAY_OBJECT_MANAGER_PORT` | `8076` | `start.sh` | Object-manager port. Head only. |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | `start.sh`; `src/ndif/cli/config.py` `DEFAULTS` | Ray dashboard port, bound on `0.0.0.0`. Head only. |
| `NDIF_RAY_DASHBOARD_GRPC_PORT` | `52366` | `start.sh` | Dashboard agent gRPC port. Head only. |
| `NDIF_RAY_METRICS_PORT` | `8080` | `start.sh` | Ray's `--metrics-export-port`, the Prometheus scrape target. Not a Serve port: NDIF does not use Ray Serve. Head only. |
| `NDIF_RAY_TEMP_DIR` | `/tmp/ray` | `start.sh` | Ray session/temp dir; `start.sh` exits with an explicit error if it isn't writable. Head and worker. |
| `NDIF_RAY_HEAD_WAIT_RETRIES` | `60` | `start.sh` `wait_for_head` | Worker: TCP connect attempts before giving up on the head. |
| `NDIF_RAY_HEAD_WAIT_INTERVAL_S` | `2` | `start.sh` `wait_for_head` | Worker: seconds between attempts. Defaults give a two-minute window. |

Ray's client-server port (10001) is not configurable: `start.sh` never passes
`--ray-client-server-port`. See [ports.md](ports.md).

## Controller and model defaults

Read by the controller launcher,
`src/ndif/services/ray/deployments/controller/controller.py`
`ControllerDeploymentArgs`, on the ray head. Each is a *default*; a per-model
`DeploymentConfig` overrides it.

| Variable | Default | Effect |
|---|---|---|
| `NDIF_DEPLOYMENTS` | `""` | Model keys to deploy at controller start, separated by `\|`. Splitting the empty default yields `[""]`, not `[]`. |
| `NDIF_MODEL_IMPORT_PATH` | `NDIF_DEFAULT_MODEL_ACTOR_CLASS`, else `ndif.services.ray.deployments.modeling.base.ModelActor` | Dotted import path of the model actor class each deployment is built from (`ControllerDeploymentArgs.model_import_path`). Resolution: `NDIF_MODEL_IMPORT_PATH` → `NDIF_DEFAULT_MODEL_ACTOR_CLASS` → the base, in-process `ModelActor`. A per-deployment `DeploymentConfig.actor_class` still wins. |
| `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | — | The legacy name, read only as the fallback above. The dev compose sets it on the `ray` service to `ndif.services.ray.sandbox.model.SandboxModelActor`, so **the compose stack sandboxes untrusted requests and a bare controller (the published image, a from-source install) does not**. |
| `NDIF_TP_MODEL_ACTOR_CLASS` | **unset — tensor parallelism off** | The actor class a tensor-parallel replica gets. Unset disables the feature outright: no sharding degree is worked out and a per-model `max_tp` does nothing. Set `ndif.services.ray.tp.model.TPModelActor`, or `ndif.services.ray.tp.model.SandboxedTPModelActor` on a cluster that takes untrusted traffic, or a subclass of either. Opt-in because a sharded replica cannot be cached and needs transformers >= 5.15. |
| `NDIF_CONTROLLER_IMPORT_PATH` | `ndif.services.ray.deployments.controller.controller.ControllerActor` | The controller actor class `app()` launches, so an operator can swap in a subclass. |
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | Seconds between `Cluster.update_nodes` passes (read in `_ControllerActor.check_nodes`). |
| `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | unset (no cap) | Per-request execution cap handed to each model actor. Unset, a request runs until it finishes. Set it on anything shared: a runaway block otherwise holds its replica indefinitely. A per-model `execution_timeout_seconds` overrides it. |
| `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` | How long a replica is protected from eviction after it comes up. |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | **Scales CPU RAM, not GPU memory**: the WARM-cache budget is the node's advertised total RAM (`src/ndif/services/ray/resources.py` `get_total_cpu_memory_bytes`) times this, in `Cluster.update_nodes`. |
| `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | Multiplicative slack in the memory estimate used to place a replica. |
| `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MiB) | Additive slack in the same estimate. |
| `NDIF_DEFAULT_DTYPE` | `bfloat16` | Dtype a model loads in when its config names none. Pinned into the config before placement (`_ControllerActor._deploy`) so the size estimate and the load agree. |

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_SANDBOX_POOL_SIZE` | `7` | `src/ndif/services/ray/sandbox/model.py` `DEFAULT_POOL_SIZE` | Runners kept pre-warmed per **sandboxed** model actor. A cold spawn (~4 s) is several times a warm execution, so a smaller pool makes a saturated queue pay the spawn inline; each warm runner holds roughly 0.5 GB of RAM whether used or not. Turn it down on memory- or core-tight nodes. A per-deployment `pool_size` overrides it. |

## Object store

Read by the API and by model actors through
`src/ndif/common/providers/objectstore.py` `ObjectStoreProvider.CONFIG`. Boto3:
RustFS in dev, S3 in production.

| Variable | Default | Effect |
|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | Server-side endpoint for uploads and bucket creation. **Empty means real AWS S3** — boto3 derives the endpoint from the region. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | `""` | Client-facing endpoint used *only for presigning*; empty falls back to the URL above. A presigned URL signs the host, so this must be the host the downloader actually hits. |
| `NDIF_OBJECT_STORE_ACCESS_KEY` | `rustfsadmin` | S3 access key; also RustFS's `RUSTFS_ACCESS_KEY` when the CLI spawns RustFS. |
| `NDIF_OBJECT_STORE_SECRET_KEY` | `rustfsadmin` | S3 secret key; likewise `RUSTFS_SECRET_KEY`. |
| `NDIF_OBJECT_STORE_BUCKET` | `ndif-results` | Bucket result blobs are written to. |
| `NDIF_OBJECT_STORE_REGION` | `us-east-1` | Set explicitly so presigning never round-trips to discover the region. |
| `NDIF_OBJECT_STORE_VERIFY` | `true` | TLS verification (`_boolish`: `1`/`true`/`yes`/`on`). Set false for a self-signed store over HTTPS. |
| `NDIF_OBJECT_STORE_CONSOLE_PORT` | `9001` | **CLI only** (`src/ndif/cli/service.py` `_rustfs_command`): the console port of a RustFS the CLI spawns. Compose hardcodes its own. |

Set both keys empty to authenticate as the host's own IAM role: the keys are used
only when **both** are set (`ObjectStoreProvider._make_client`); otherwise boto3
walks its own credential chain.

## Postgres and API-key auth

Read by `src/ndif/common/providers/postgres.py` `PostgresProvider.CONFIG`.

| Variable | Default | Effect |
|---|---|---|
| `NDIF_POSTGRES_URL` | `""` | **The auth switch.** Set → keys are checked against the `keys` table, failing closed (503) if the DB is unreachable; set without `asyncpg` installed, the API raises rather than run unauthenticated (`PostgresProvider.connect`). Unset → no auth, and a request that doesn't send `trusted` is stamped trusted. What that means for where user code runs is in [auth-and-limits.md](../concepts/auth-and-limits.md). |
| `NDIF_POSTGRES_POOL_MIN` | `1` | Connections opened up front on first use. |
| `NDIF_POSTGRES_POOL_MAX` | `10` | Pool ceiling under concurrency. |
| `NDIF_POSTGRES_COMMAND_TIMEOUT_S` | `10.0` | Per-statement timeout, so a wedged DB surfaces as an error. |

## Metrics (InfluxDB)

Read by `src/ndif/common/providers/influx.py` `InfluxProvider.CONFIG`, and by the
dashboard's report cron (`src/ndif/services/dashboard/jobs/report.py` `parse_args`)
for the URL, token, org and bucket. The provider is fail-open.

| Variable | Default | Effect |
|---|---|---|
| `NDIF_INFLUX_URL` | `""` (metrics off) | InfluxDB v2 endpoint. Unset, the provider never connects. |
| `NDIF_INFLUX_TOKEN` | `""` | Write token. Empty means Influx rejects writes; the service keeps running. |
| `NDIF_INFLUX_ORG` | `ndif` | Organization. |
| `NDIF_INFLUX_BUCKET` | `metrics` | Bucket. |
| `NDIF_INFLUX_ENABLED` | `true` | Off-switch independent of the URL (`_boolish`); short-circuits `InfluxProvider.connect`. |
| `NDIF_INFLUX_BATCH_SIZE` | `500` | Points buffered before a flush. |
| `NDIF_INFLUX_FLUSH_INTERVAL_MS` | `1000` | Max ms between flushes; whichever threshold hits first. |
| `NDIF_INFLUX_TIMEOUT_MS` | `10000` | Per-write HTTP timeout. |

## Logs (Loki)

Read by `src/ndif/common/providers/loki.py` `LokiProvider.CONFIG`.

| Variable | Default | Effect |
|---|---|---|
| `NDIF_LOKI_URL` | `""` | **The log-shipping switch.** Empty → `logging_loki` is never imported; the console handler is configured either way (`LokiProvider.connect`). Set to a push endpoint (`http://loki:3100/loki/api/v1/push`) to enable; set without the package installed, logs stay console-only with a warning. |
| `NDIF_LOKI_LEVEL` | `INFO` | Minimum level shipped; the console keeps `NDIF_LOG_LEVEL`. |
| `NDIF_LOKI_QUEUE_MAX` | `10000` | In-memory buffer bound, so a Loki outage drops records rather than growing. |

## Dashboard

`NDIF_DASHBOARD_*` fields map onto `src/ndif/services/dashboard/backend/config.py`
`Settings` by `env_prefix`; the port and cron schedules are read by
`src/ndif/services/dashboard/start.sh`. Cron strips the environment, so
`start.sh` writes an explicit variable block into `/etc/cron.d/ndif-dashboard`;
only the variables listed there reach a cron job (`NDIF_API_URL`, `NDIF_API_KEY`,
`NDIF_RAY_ADDRESS`, `NDIF_REDIS_URL`, the four `NDIF_INFLUX_*` above,
`NDIF_ENVIRONMENT`, `HF_TOKEN`, `HF_HOME`). A new one a job depends on means
editing that heredoc.

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_DASHBOARD_PORT` | `8081` | `start.sh` | Port uvicorn binds, on `0.0.0.0`. |
| `NDIF_DASHBOARD_USERNAME` | `admin` | `Settings.username` | The single admin username. |
| `NDIF_DASHBOARD_PASSWORD_HASH` | `""` | `Settings.password_hash` | Bcrypt hash of the admin password: `python -m ndif.services.dashboard.backend.auth hash <password>`. |
| `NDIF_DASHBOARD_SESSION_SECRET` | `change-me-please-this-is-not-secure` | `Settings.session_secret` | Cookie-signing secret; rotating it ends every session. Set it before exposing the dashboard. |
| `NDIF_DASHBOARD_SESSION_TTL_DAYS` | `7` | `Settings.session_ttl_days` | Session cookie lifetime. |
| `NDIF_DASHBOARD_DEV_MODE` | `false` | `Settings.dev_mode` | **`true` bypasses auth on every route.** The dev compose sets it; remove it for any real deployment. |
| `NDIF_DASHBOARD_DATA_DIR` | `~/ndif_dashboard` | `Settings.data_dir`; `src/ndif/services/dashboard/jobs/util.py` `DEFAULT_DATA_DIR`; `start.sh` | Root for `logs/`, `schedule.json`, `.reconcile.state.json`, `config.json`, `cache/`. |
| `NDIF_DASHBOARD_FRONTEND_DIST` | `<package>/frontend/dist` | `Settings.frontend_dist` | Built Vue SPA directory to serve. |
| `NDIF_DASHBOARD_MONITOR_URL` | `http://localhost:8001` | `start.sh` | What the monitor cron probes. Point it at the *public* URL so the probe covers DNS, TLS and the load balancer. |
| `NDIF_DASHBOARD_MONITOR_CRON` | `*/10 * * * *` | `start.sh` | Schedule of the uptime monitor. |
| `NDIF_DASHBOARD_RECONCILE_CRON` | `*/2 * * * *` | `start.sh` | Schedule of the schedule reconciler. |
| `NDIF_DASHBOARD_REPORT_CRON` | `0 0 * * *` | `start.sh` | Schedule of the daily usage/uptime digest (`src/ndif/services/dashboard/jobs/report.py`). |
| `NDIF_DASHBOARD_REPORT_WINDOW_HOURS` | `24` | `start.sh`; `report.py` `parse_args` | How far back the digest looks. |

## Switches where empty means off

| Variable | Unset/empty behavior |
|---|---|
| `NDIF_POSTGRES_URL` | No API-key auth, and requests that don't say otherwise are trusted — see [auth-and-limits.md](../concepts/auth-and-limits.md). |
| `NDIF_LOKI_URL` | Console logs only. |
| `NDIF_INFLUX_URL` / `NDIF_INFLUX_ENABLED=false` | No metrics; a bad token means writes are rejected and dropped. The service keeps running. |
| `NDIF_MIN_NNSIGHT_VERSION` / `NDIF_MIN_PYTHON_VERSION` | No client version gating. |
| `NDIF_RAY_HEAD_ADDRESS` | This node starts a Ray head rather than joining one. |
| `NDIF_TP_MODEL_ACTOR_CLASS` | No tensor parallelism. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | Presigned URLs carry the internal endpoint — clients outside the network can't download large results. |
| `NDIF_OBJECT_STORE_URL` | boto3 talks to real AWS S3, not a local RustFS. |
| `NDIF_OBJECT_STORE_ACCESS_KEY` **and** `_SECRET_KEY` | boto3's own credential chain (environment, shared config, container or instance role). |

## Minimum viable config

Standing up your own NDIF rather than running the dev compose, this is the set
that has to change. Check the result with `ndif doctor`, which probes Redis, the
object store, the API and Ray from these same variables
(`src/ndif/cli/commands/doctor.py` `_report_connectivity`).

**Every service:**

```bash
NDIF_REDIS_URL=redis://redis.internal:6379
NDIF_ENVIRONMENT=prod
NDIF_LOKI_URL=http://loki.internal:3100/loki/api/v1/push   # optional
NDIF_INFLUX_URL=http://influx.internal:8086                # optional
NDIF_INFLUX_TOKEN=...
```

**API service:**

```bash
NDIF_RAY_ADDRESS=ray://ray-head.internal:10001
NDIF_POSTGRES_URL=postgresql://ndifapi:<pw>@postgres.internal:5432/ndif
NDIF_OBJECT_STORE_URL=https://s3.internal:9000        # what the server uploads to
NDIF_OBJECT_STORE_PUBLIC_URL=https://results.example.org  # what the client downloads from
NDIF_OBJECT_STORE_ACCESS_KEY=...
NDIF_OBJECT_STORE_SECRET_KEY=...
NDIF_MIN_NNSIGHT_VERSION=0.5.0                        # reject clients you can't serve
```

**Ray head** — object-store config must match the API's; the model actor uploads
results and signs the URL:

```bash
NDIF_REDIS_URL=redis://redis.internal:6379   # never leave this at localhost
NDIF_OBJECT_STORE_URL=https://s3.internal:9000
NDIF_OBJECT_STORE_PUBLIC_URL=https://results.example.org
NDIF_OBJECT_STORE_ACCESS_KEY=...
NDIF_OBJECT_STORE_SECRET_KEY=...
NDIF_MODEL_IMPORT_PATH=ndif.services.ray.sandbox.model.SandboxModelActor
NDIF_RAY_TEMP_DIR=/var/lib/ray            # if /tmp is small or tmpfs-backed
```

**Ray worker** — everything the head has, plus the join address:

```bash
NDIF_RAY_HEAD_ADDRESS=ray-head.internal:6385
```

**Dashboard**, if you run it:

```bash
NDIF_DASHBOARD_USERNAME=...
NDIF_DASHBOARD_PASSWORD_HASH=$2b$12$...   # python -m ndif.services.dashboard.backend.auth hash <pw>
NDIF_DASHBOARD_SESSION_SECRET=<32+ random bytes>
NDIF_DASHBOARD_MONITOR_URL=https://api.example.org  # public URL
NDIF_API_KEY=<a real client key>          # the monitor cron's traces need one
# and do NOT set NDIF_DASHBOARD_DEV_MODE
```

## Non-NDIF environment the code reads or sets

| Variable | Where | Effect |
|---|---|---|
| `RAY_ADDRESS` | `src/ndif/services/ray/start.sh` | Ray's own variable; the head path `unset`s it so `ray start --head` creates a cluster instead of attaching to one. |
| `GRPC_ENABLE_FORK_SUPPORT` | `src/ndif/services/ray/start.sh` | Defaulted to `0` before `ray start` so the Ray client proxier's per-client fork doesn't die intermittently. An explicit value wins. |
| `RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES`, `PYTORCH_CUDA_ALLOC_CONF` | set by `Deployment.create` | `1`: Ray doesn't mask GPUs from a model actor, which targets its GPUs through `max_memory` instead. `expandable_segments:True`: less CUDA fragmentation. |
| `RANK`, `LOCAL_RANK`, `WORLD_SIZE`, `MASTER_ADDR`, `MASTER_PORT` | set per rank by `src/ndif/services/ray/tp/common.py` `rank_env` | A tensor-parallel group's rendezvous: `127.0.0.1` and an OS-chosen free port (`ShardGroup.__init__`). |
| `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` | written by `src/ndif/cli/service.py` `_rustfs_env`; set on the compose `rustfs` service | RustFS reads its root credentials from these; the CLI mirrors the object-store keys into them. |
| `HF_TOKEN`, `HF_HOME` | compose `ray` service (`HF_TOKEN` passthrough, HF cache bind mount); the dashboard cron block | Hugging Face auth and cache, read by `transformers` / `huggingface_hub`. The image declares `/root/.cache/huggingface` a `VOLUME`, and compose bind-mounts the host cache there, so weights persist. |
| `HOME` | `docker/Dockerfile` `ENV` | `/root` for every uid; `/root`, `/root/.cache` and the HF cache are world-writable (sticky), so `HF_HOME`, `NDIF_HOME` and library caches under `~` resolve to writable paths under `docker run --user`. |
| `NDIF_USER` | compose `ray` service `user:` | The `uid:gid` the compose `ray` container runs as; default `0:0` (root). `NDIF_USER=$(id -u):$(id -g) just up` keeps the bind-mounted HF cache yours. |
| `PYTHONPATH` and the rest of a sandbox runner's environment | `src/ndif/services/ray/sandbox/host.py` `spawn` | A runner inherits only the allowlist in `RUNNER_ENV` (no credentials, no rank variables), plus the package root on `PYTHONPATH` and `PYTHONHASHSEED=0`. |
| `NDIF_VERSION` | a `docker build` ARG in `docker/Dockerfile`, not read at runtime | Carries the version into the build (setuptools-scm has no `.git` there); `VCS_REF` and `BUILD_DATE` are its siblings. Resolved versions go to `/etc/ndif/build.json` via `ndif version --write`. |
| `DOCKER_INFLUXDB_INIT_*`, `POSTGRES_*`, `GF_*` | compose `influxdb`, `postgres`, `grafana` services | Image bootstrap variables; keep them consistent with the `NDIF_INFLUX_*` and `NDIF_POSTGRES_URL` the services use. |

## Related

- [configuration.md](../operating/configuration.md) — the config model and precedence in prose.
- [ports.md](ports.md) — which variables move a port.
- [compose-stack.md](../operating/compose-stack.md) — the per-container `environment:` blocks.
- [production.md](../operating/production.md) — what to change when you leave the dev compose.
- [providers.md](../developing/providers.md) — how `CONFIG` specs become class attributes.
- [auth-and-limits.md](../concepts/auth-and-limits.md) — what `NDIF_POSTGRES_URL` and the `NDIF_MIN_*` gates mean for a user.
