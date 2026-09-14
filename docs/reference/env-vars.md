---
title: Environment Variables
one_liner: Every NDIF_* variable the server reads — default, the exact line that reads it, and what it changes.
tags: [reference, operating, config]
related: [docs/operating/configuration.md, docs/reference/ports.md, docs/operating/compose-stack.md, docs/operating/production.md, docs/developing/providers.md, docs/concepts/auth-and-limits.md, docs/operating/observability.md, docs/gotchas/networking-and-compose.md]
sources: [src/ndif/cli/config.py, src/ndif/common/providers/redis.py, src/ndif/common/providers/ray.py, src/ndif/common/providers/objectstore.py, src/ndif/common/providers/postgres.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/influx.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/api/versioning.py, src/ndif/services/api/queue/config.py, src/ndif/services/ray/start.sh, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/dashboard/start.sh, src/ndif/services/dashboard/backend/config.py, src/ndif/common/redis/env.py, src/ndif/common/redis/status.py, src/ndif/common/logging_setup.py, docker/docker-compose.yml]
---

# Environment Variables

## What this covers

Every environment variable any NDIF process reads, grouped by subsystem, with the
file and line that reads it. Every row comes from a line of code, not from the
README's table, which has drifted in a few places.

## The config model

**There is no config file.** No YAML, no TOML, no `settings.py`. Every knob is an
environment variable, most of them read at import — by a provider's `CONFIG` spec
(`src/ndif/common/providers/base.py:41-43`) or a bare `os.environ.get`. Two
consequences: changing a value needs a process restart, and a variable missing
from a process's environment falls back to a hardcoded default, never to another
service's value. Four layers stack up, later winning:

1. **Code defaults**, in one of four shapes: the second element of a provider
   `CONFIG` tuple `("NDIF_X", default, cast)`; the second argument to
   `os.environ.get`; a `${VAR:-default}` fallback in a `start.sh`; or a pydantic
   `Field(default=...)` on the dashboard's `Settings`, matched to a variable by
   `env_prefix = "NDIF_DASHBOARD_"` (`dashboard/backend/config.py:33`).
2. **CLI defaults** — `DEFAULTS` in `src/ndif/cli/config.py:22-31`, overlaid
   *beneath* the real environment by `build_env` (`config.py:60`). These exist
   because a few code defaults are wrong for a single-host run: Ray's own GCS
   port is 6379, the same as Redis, so the CLI moves it to 6385.
3. **`.env` files** — `config.load_env_files` (`config.py:42-46`) runs before any
   command. A CWD-relative `.env` is loaded without `override`, so it only fills
   gaps in the shell environment; an explicit `--env-file` is loaded with
   `override=True` and therefore beats the shell.
4. **Per-process overrides** — `ndif start -e KEY=VALUE`, the typed shortcuts
   (`--redis-url`, `--ray-address`, `--ray-head-address`, `--api-port`;
   `config.py:34-39`), and in Docker the per-container `environment:` block in
   `docker/docker-compose.yml`.

Containers run `ndif start --foreground` as their entrypoint
(`docker/Dockerfile:49`), so **the CLI defaults apply inside compose too**. For
`NDIF_RAY_HEAD_PORT` it doesn't even matter which layer wins: both the CLI and
`ray/start.sh:60`'s own fallback default to 6385. Config also crosses one process
boundary automatically:
creating a model actor exports the controller's Redis, object-store, Loki and
Influx settings into the actor's Ray `runtime_env` (`cluster/deployment.py:16-39`).

## Identity and logging

Read by every service.

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_SERVICE` | `api` (image `ENV`) | `src/ndif/cli/service.py:85`, `providers/loki.py:142`, `providers/influx.py:74` | Which service(s) `ndif start` brings up when no argument is given, **and** the `service` label on every log line and metric point. The start scripts export it (`api/start.sh:10`, `ray/start.sh:17`); the controller overrides it to `model` for actor processes (`cluster/deployment.py:187`). Accepts a space/comma list. |
| `NDIF_ENVIRONMENT` | `dev` | `providers/loki.py:143`, `providers/influx.py:75` | Second static label on all telemetry — the prod/staging/dev discriminator in Grafana. |
| `NDIF_LOG_LEVEL` | `INFO` | `src/ndif/common/logging_setup.py:127` | Level of the `ndif` logger and its console handler. Applied only when `configure_console` runs, and only if it *lowers* the current level. Not the root logger — `propagate` is set to `False` (`logging_setup.py:125`). |
| `NDIF_HOME` | `~/.ndif` | `src/ndif/cli/state.py:29`, `cli/config.py:23`, `cli/service.py:39` | CLI state root: `run/<name>.pid`, `logs/<name>.log`, and MinIO's data dir when the CLI spawns it. CLI-only. |

## API service

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_API_PORT` | `8001` | `src/ndif/services/api/gunicorn_conf.py:29` | Port gunicorn binds, always on `0.0.0.0`. |
| `NDIF_API_WORKERS` | `1` | `gunicorn_conf.py:30` | Uvicorn worker count. The queue dispatcher is started once in the master regardless (`gunicorn_conf.py:61-66`), so raising this scales ingress only. |
| `NDIF_API_TIMEOUT` | `120` | `gunicorn_conf.py:32` | Gunicorn worker timeout in seconds. A long-running websocket session is not a request, but a slow upload is. |
| `NDIF_API_URL` | `http://localhost:8001` | `cli/config.py:26`, `cli/commands/env.py:46`, `cli/commands/doctor.py:87`, `dashboard/backend/config.py:51` | Where *clients of the API* look for it: the CLI's `env`/`info`/`doctor` checks and the dashboard's `/api/status` proxy. Does not affect what the API binds. |
| `NDIF_MIN_NNSIGHT_VERSION` | unset | `src/ndif/services/api/versioning.py:23` | Minimum client nnsight version. **Unset or empty disables nnsight gating entirely** (`versioning.py:23` coerces `""` to `None`); set it and an older client gets a 400 at submit. |
| `NDIF_MIN_PYTHON_VERSION` | unset | `versioning.py:24` | Same, for the client's Python version. |
| `NDIF_API_KEY` | unset | `src/ndif/services/dashboard/jobs/monitor.py:378`, `dashboard/start.sh:62` | A *client* key, used only by the dashboard's monitor cron so its synthetic model traces can authenticate. Nothing in the API or CLI reads it. |

## Queue and autoscaling

Read once at import into a frozen dataclass (`queue/config.py:72`), in the API's
dispatcher process only.

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_QUEUE_KEY` | `queue` | `src/ndif/services/api/queue/config.py:58` | Redis list the API `LPUSH`es onto and the dispatcher pops from. |
| `NDIF_QUEUE_FETCH_TIMEOUT_S` | `10` | `queue/config.py:59` | Blocking-pop timeout; bounds how long an idle dispatch loop waits before re-checking evictions and errors. |
| `NDIF_QUEUE_FETCH_BATCH_MAX` | `32` | `queue/config.py:60` | Max requests drained per dispatch iteration. |
| `NDIF_AUTOSCALING_INTERVAL_S` | `5` | `queue/config.py:61` | How often a per-model processor inspects its queue head. |
| `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` | `30` | `queue/config.py:63` | Scale up when the oldest queued request has waited longer than this. |
| `NDIF_AUTOSCALING_BACKOFF_S` | `120` | `queue/config.py:65` | Pause after a scale-up before another can fire, so the new replica gets a chance to drain. |
| `NDIF_AUTOSCALING_MAX_REPLICAS` | `3` | `queue/config.py:67` | Per-model replica ceiling for autoscaling. |

All six numeric vars go through `_positive_int` (`queue/config.py:12-22`), which
raises at import on a non-integer or non-positive value — the API won't boot.

## Redis and the shared caches

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_REDIS_URL` | `redis://localhost:6379` | `src/ndif/common/providers/redis.py:23`, `cli/service.py:31` | The one Redis every service shares: request queue, response pub/sub, status/env caches, trigger streams. The CLI also derives the port it starts `redis-server` on from this URL. |
| `NDIF_ENV_TTL_S` | `300` | `src/ndif/common/redis/env.py:33` | How long a cached `/env` payload is served before a refresh is triggered. |
| `NDIF_ENV_TIMEOUT_S` | `60` | `common/redis/env.py:37` | How long a `/env` request waits for a refresh, and how long the refresh worker holds the coalescing lock. |
| `NDIF_STATUS_TTL_S` | `60` | `src/ndif/common/redis/status.py:27` | Same, for the cached `/status` blob. |
| `NDIF_STATUS_TIMEOUT_S` | `60` | `common/redis/status.py:31` | Same, for a waiting `/status` request. |

> **Gotcha:** the ray container **must** be given `NDIF_REDIS_URL` explicitly
> (`docker/docker-compose.yml:213`). The default `localhost:6379` inside that
> container is Ray's own GCS, not Redis, so the response handshake fails in a way
> that looks like a hung request.

## Ray node

Read by `src/ndif/services/ray/start.sh` on the ray service only — except
`NDIF_RAY_ADDRESS`, read by every service that talks *to* Ray.

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | `src/ndif/common/providers/ray.py:46`, `cli/config.py:28` | The `ray://` **client** address the API, dashboard and CLI connect with. Says nothing about head-vs-worker. A URL with no port logs a warning and falls back to 6379 (`providers/ray.py:65-70`). |
| `NDIF_RAY_HEAD_ADDRESS` | unset | `ray/start.sh:27`, `cli/commands/start.py:134` | **The head/worker switch.** Unset → `ray start --head`. Set to the head's `HOST:PORT` → wait for it, then join as a worker. A `ray://` prefix is stripped (`start.sh:74`). When it's set, a bare `ndif start` brings up *only* Ray. |
| `NDIF_RAY_HEAD_PORT` | `6385` | `cli/config.py:29`, `ray/start.sh:60` | Ray's GCS port on the head (`--port`), deliberately offset from Redis's 6379. Head only. Both the CLI and `ray/start.sh:60`'s own `${NDIF_RAY_HEAD_PORT:-6385}` fallback default to 6385, so a hand-run `start.sh` and `ndif start` agree and neither collides with a local Redis. |
| `NDIF_RAY_OBJECT_MANAGER_PORT` | `8076` | `ray/start.sh:61` | Ray's object-manager port. Head only. |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | `ray/start.sh:64`, `cli/config.py:30` | Ray dashboard port, bound on `0.0.0.0` (`start.sh:63`). Head only. |
| `NDIF_RAY_DASHBOARD_GRPC_PORT` | `52366` | `ray/start.sh:65` | Dashboard agent gRPC port. Head only. |
| `NDIF_RAY_METRICS_PORT` | `8080` | `ray/start.sh:66` | Ray's `--metrics-export-port` — the Prometheus scrape endpoint, and what `prometheus.yml:20` targets as `ray:8080`. It is not a Serve HTTP port: nothing in `src/` imports `ray.serve`, the dependency is `ray[default]` (`pyproject.toml:44`), and model deployments are plain detached Ray actors. Head only. |
| `NDIF_RAY_TEMP_DIR` | `/tmp/ray` | `ray/start.sh:19` | Ray session/temp dir. `start.sh:21-25` exits with an explicit error if it isn't writable. Head and worker. |
| `NDIF_RAY_HEAD_WAIT_RETRIES` | `60` | `ray/start.sh:33` | Worker: TCP connect attempts before giving up on the head. |
| `NDIF_RAY_HEAD_WAIT_INTERVAL_S` | `2` | `ray/start.sh:34` | Worker: seconds between those attempts. Defaults give a two-minute boot window. |

Ray's client-server port (10001) is **not** env-configurable: `start.sh` never
passes `--ray-client-server-port`. See `docs/reference/ports.md`.

## Controller and model defaults

Read by the controller launcher (`ControllerDeploymentArgs`) on the ray head. Each
is only a *default*; a per-model `DeploymentConfig` overrides it.

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_DEPLOYMENTS` | `""` | `src/ndif/services/ray/deployments/controller/controller.py:533` | Model keys to deploy at controller start, separated by a pipe character. Note that splitting the empty default yields `[""]`, not `[]`. |
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | `controller.py:113` | Seconds between `Cluster.update_nodes` reconcile passes. |
| `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | unset (no cap) | `controller.py:623-631` | Per-request execution cap handed to each model actor. Unset means a request runs until it finishes, so a long job is never cut off on a single-user deployment. Set it on anything shared: with no cap a runaway block holds its replica indefinitely, and `NDIF_AUTOSCALING_MAX_REPLICAS` (3) of those take the model out. A per-model `execution_timeout_seconds` overrides it either way. |
| `NDIF_MODEL_IMPORT_PATH` | falls back to `NDIF_DEFAULT_MODEL_ACTOR_CLASS`, then `ndif.services.ray.deployments.modeling.base.ModelActor` | `controller.py:554-560` | Dotted import path of the model actor class the controller builds each deployment from. Resolution order: `NDIF_MODEL_IMPORT_PATH` → `NDIF_DEFAULT_MODEL_ACTOR_CLASS` → the base `ModelActor`. A per-deployment `DeploymentConfig.actor_class` still overrides it (`_ControllerActor.__init__` stores it as `default_model_actor_class`). |
| `NDIF_CONTROLLER_IMPORT_PATH` | `ndif.services.ray.deployments.controller.controller.ControllerActor` | `controller.py:563-566` | Dotted import path of the controller actor class `app()` launches (`_import_from_path`, `controller.py:597`). Lets an operator swap in a `ControllerActor` subclass without editing the module. |
| `NDIF_TP_MODEL_ACTOR_CLASS` | **unset — tensor parallelism off** | `controller.py` | The actor class a *tensor-parallel* replica gets. Unset is not a fallback to the built-in one: it disables the feature outright, so no sharding degree is worked out, no GPU count is rounded up to one a model splits into evenly, and a per-model `max_tp` does nothing. Set it to `ndif.services.ray.tp.model.TPModelActor` to enable it, to `ndif.services.ray.tp.model.SandboxedTPModelActor` if the cluster takes untrusted traffic (same trusted path, untrusted blocks run in one runner the group hosts), or to a subclass of either. Opt-in because a sharded replica cannot be cached and needs transformers >= 5.15. |
| `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | `ndif.services.ray.deployments.modeling.base.ModelActor` | `controller.py:556-558` | Now the **fallback** for `NDIF_MODEL_IMPORT_PATH` rather than an independent knob. The dev compose still sets it to `ndif.services.ray.sandbox.model.SandboxModelActor` (`docker/docker-compose.yml:228`), which — absent `NDIF_MODEL_IMPORT_PATH` — is what wins, so **the compose stack runs sandboxed by default and a bare controller does not**. |
| `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` | `controller.py:544` | How long a replica is protected from eviction after it comes up. |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | `controller.py:547` | **Scales CPU RAM, not GPU memory.** It is the WARM-cache budget: `cluster.py:106-108` multiplies the node's advertised `cpu_memory_bytes` by it. `resources.py:20-24` advertises *total* RAM deliberately (so the budget is stable regardless of what else is resident), and this scales it down. `cuda_memory_bytes` is untouched by this variable. |
| `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | `controller.py:550` | Multiplicative slack in the memory estimate used to place a replica. |
| `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MiB) | `controller.py:553` | Additive slack in the same estimate. |
| `NDIF_DEFAULT_DTYPE` | `bfloat16` | `controller.py:555` | Dtype a model loads in when its config doesn't name one. Pinned into the config before placement (`controller.py:127-129`) so the size estimate and the actual load agree. |
| `NDIF_SANDBOX_POOL_SIZE` | `7` | `sandbox/model.py:DEFAULT_POOL_SIZE` | Runners kept pre-warmed per **sandboxed** model actor (ignored by the base, in-process actor). Sized from the costs it trades: a cold spawn is ~4s against a ~0.7s warm execution, so the pool must be at least spawn/execute ≈ 6 or a saturated queue drains it and requests pay the spawn inline. Each warm runner holds ~420 MB (PSS) whether used or not — 7 is ~2.9 GB per model actor — and refills contend for CPU on the actor's node. Turn it down on memory- or core-tight nodes, or when many models are resident at once. A per-deployment `pool_size` kwarg still overrides it. |
| `NDIF_MAX_SOCKET_RESULT_BYTES` | *(unset)* | `controller.py:ControllerDeploymentArgs.max_socket_result_bytes`, forwarded into each actor's env by `cluster/deployment.py` | Largest result, **after compression**, handed back on the COMPLETED response itself instead of staged in the object store for the client to download. Unset means no limit, so every result for a blocking request travels over the websocket. A result above the limit — and every result for a non-blocking request, which has no live socket — goes to the object store and the client gets a presigned url as before. The ceiling worth knowing is redis: a pubsub client whose output buffer exceeds `client-output-buffer-limit pubsub` (32 MB hard by default) is disconnected, which drops the response. Set this below that if results can be large. A value that is not a positive integer is ignored with a warning. |

## Object store

Read by the API and by model actors. Boto3-backed: MinIO in dev, real S3 in prod.

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | `src/ndif/common/providers/objectstore.py:41` | Server-side endpoint used to upload and ensure the bucket. **Empty means real AWS S3** — boto3 derives the endpoint from `region`. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | `""` | `objectstore.py:43` | Client-facing endpoint used *only for presigning*. Empty → falls back to `url`. A presigned URL is an HMAC over the request including the host, so this must be the host the downloader actually hits. |
| `NDIF_OBJECT_STORE_ACCESS_KEY` | `minioadmin` | `objectstore.py:47` | S3 access key. Also becomes MinIO's `MINIO_ROOT_USER` when the CLI spawns MinIO (`cli/service.py:48`). **Set this and the secret key both empty** to authenticate as the host's own IAM role instead — see below. |
| `NDIF_OBJECT_STORE_SECRET_KEY` | `minioadmin` | `objectstore.py:48` | S3 secret key; likewise `MINIO_ROOT_PASSWORD` (`cli/service.py:49`). |
| `NDIF_OBJECT_STORE_BUCKET` | `ndif-results` | `objectstore.py:46` | Bucket result blobs are written to. |
| `NDIF_OBJECT_STORE_REGION` | `us-east-1` | `objectstore.py:49` | Set explicitly so presigning never round-trips to discover the region. |
| `NDIF_OBJECT_STORE_VERIFY` | `true` | `objectstore.py:51` | TLS verification. Set false for self-signed MinIO over HTTPS. Parsed by `_boolish` — `1`/`true`/`yes`/`on` (`objectstore.py:33`). |
| `NDIF_OBJECT_STORE_CONSOLE_PORT` | `9001` | `src/ndif/cli/service.py:38` | **CLI only** — the console port for a MinIO server the CLI spawns. The compose file hardcodes `--console-address ":9001"` (`docker-compose.yml:119`) and ignores this. |

## Postgres and API-key auth

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_POSTGRES_URL` | `""` | `src/ndif/common/providers/postgres.py:56` | **The auth master switch — and, indirectly, the isolation switch.** See the callout below. Set → keys are checked against the `keys` table, failing *closed* (503) if the DB is unreachable. If it's set and `asyncpg` isn't installed the API raises rather than silently disabling auth (`postgres.py:91-95`). |
| `NDIF_POSTGRES_POOL_MIN` | `1` | `postgres.py:59` | Connections opened up front on first use. |
| `NDIF_POSTGRES_POOL_MAX` | `10` | `postgres.py:60` | Pool ceiling under concurrency. |
| `NDIF_POSTGRES_COMMAND_TIMEOUT_S` | `10.0` | `postgres.py:63` | Per-statement timeout, so a wedged DB surfaces as an error instead of hanging requests. |

> **Gotcha — leaving `NDIF_POSTGRES_URL` unset does more than disable auth.**
> With no Postgres configured, `verify_api_key` returns `None` and
> `validate_request` stamps `request.trusted = True` on **every** request
> (`src/ndif/services/api/auth.py:172-174`). Two things follow from that flag:
>
> - **The sandbox is skipped.** `SandboxModelActor.execute` runs a trusted
>   request in-process via the base actor instead of handing it to a runner
>   subprocess (`sandbox/model.py:242-243`; same branch in `execution_scope`,
>   `model.py:184`). User code executes inside the model actor, next to the weights.
> - **Models load with `trust_remote_code`.** The flag rides from
>   `request.trusted` through `Processor.ensure_started`
>   (`queue/processor.py:114, 153-154`) into the `DeploymentConfig` the replica
>   deploys with (`queue/replica.py:103`) and on to the HF load
>   (`controller/cluster/cluster.py:169`).
>
> With Postgres configured, `trusted` instead comes from the key's `trusted`
> user_tag (`auth.py:75-76, 170`), so ordinary users are sandboxed. **Self-hosting
> NDIF anywhere but a trusted LAN means setting `NDIF_POSTGRES_URL`.** The dev
> compose leaves it commented out (`docker-compose.yml:156`), so the dev stack is
> unauthenticated *and* unsandboxed.

## Metrics (InfluxDB)

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_INFLUX_URL` | `http://localhost:8086` | `src/ndif/common/providers/influx.py:66` | InfluxDB v2 endpoint. |
| `NDIF_INFLUX_TOKEN` | `""` | `influx.py:67` | Write token. Empty means writes will be rejected by Influx; the provider is fail-open, so the service keeps running without metrics. |
| `NDIF_INFLUX_ORG` | `ndif` | `influx.py:68` | Influx organization. |
| `NDIF_INFLUX_BUCKET` | `metrics` | `influx.py:69` | Target bucket. |
| `NDIF_INFLUX_ENABLED` | `true` | `influx.py:70` | Hard off-switch for metric writes, independent of the URL. `_boolish` (`influx.py:58`). |
| `NDIF_INFLUX_BATCH_SIZE` | `500` | `influx.py:78` | Points buffered before a flush. |
| `NDIF_INFLUX_FLUSH_INTERVAL_MS` | `1000` | `influx.py:79` | Max ms between flushes; flush fires on whichever threshold hits first. |
| `NDIF_INFLUX_TIMEOUT_MS` | `10000` | `influx.py:80` | Per-write HTTP timeout. |

## Logs (Loki)

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_LOKI_URL` | `""` | `src/ndif/common/providers/loki.py:139` | **The log-shipping switch.** Empty → `logging_loki` is never imported and nothing ships; the readable console handler is configured either way (`loki.py:177-180`). Set it to a push endpoint (`http://loki:3100/loki/api/v1/push`) to enable. If it's set but the package is missing, you get a warning and console-only logs (`loki.py:186-191`). |
| `NDIF_LOKI_LEVEL` | `INFO` | `loki.py:145` | Minimum level shipped to Loki; the console keeps its own level from `NDIF_LOG_LEVEL`. |
| `NDIF_LOKI_QUEUE_MAX` | `10000` | `loki.py:147` | In-memory buffer bound, so a Loki outage drops records instead of growing without limit. |

## Dashboard

Dashboard service only; `NDIF_DASHBOARD_*` maps onto pydantic-settings fields via
`env_prefix` (`backend/config.py:33`).

| Variable | Default | Read by | Effect |
|---|---|---|---|
| `NDIF_DASHBOARD_PORT` | `8081` | `src/ndif/services/dashboard/start.sh:33` | Port uvicorn binds, on `0.0.0.0` (`start.sh:87`). |
| `NDIF_DASHBOARD_USERNAME` | `admin` | `dashboard/backend/config.py:35` | The single admin username. |
| `NDIF_DASHBOARD_PASSWORD_HASH` | `""` | `backend/config.py:36` | Bcrypt hash of the admin password. Generate with `python -m ndif.services.dashboard.backend.auth hash <password>`. |
| `NDIF_DASHBOARD_SESSION_SECRET` | `change-me-please-this-is-not-secure` | `backend/config.py:37` | Cookie-signing secret. Rotating it invalidates every session. Set this before exposing the dashboard. |
| `NDIF_DASHBOARD_SESSION_TTL_DAYS` | `7` | `backend/config.py:38` | Session cookie lifetime. |
| `NDIF_DASHBOARD_DEV_MODE` | `false` | `backend/config.py:39` | **`true` bypasses auth on every route.** The dev compose sets it (`docker-compose.yml:192`); remove it for any real deployment. |
| `NDIF_DASHBOARD_DATA_DIR` | `~/ndif_dashboard` | `backend/config.py:41`, `dashboard/jobs/util.py:16`, `dashboard/start.sh:34` | Root for `logs/`, `schedule.json`, `.reconcile.state.json`, `config.json`, `cache/`. Compose points it at the `dashboard_data` volume. |
| `NDIF_DASHBOARD_FRONTEND_DIST` | `<package>/frontend/dist` | `backend/config.py:42-44` | Built Vue SPA directory to serve. |
| `NDIF_DASHBOARD_MONITOR_URL` | `http://localhost:8001` | `dashboard/start.sh:68` | What the monitor cron probes. In prod point it at the *public* URL so the probe exercises DNS, TLS and the load balancer. |
| `NDIF_DASHBOARD_MONITOR_CRON` | `*/10 * * * *` | `dashboard/start.sh:68` | Crontab expression for the uptime monitor. |
| `NDIF_DASHBOARD_RECONCILE_CRON` | `*/2 * * * *` | `dashboard/start.sh:69` | Crontab expression for the schedule reconciler. |
| `NDIF_DASHBOARD_REPORT_CRON` | `0 0 * * *` | `start.sh` | Schedule for the daily usage/uptime digest posted to Discord (`jobs/report.py`). |
| `NDIF_DASHBOARD_REPORT_WINDOW_HOURS` | `24` | `start.sh`, `jobs/report.py` | How far back that digest looks. |

`cron` strips the environment, so `start.sh:57-70` writes an explicit variable
block into `/etc/cron.d/ndif-dashboard`. Only the variables listed there reach a
cron job — a new one the jobs depend on means editing that heredoc.

## Switches where empty means off

An unset value silently disables a subsystem rather than erroring.

| Variable | Unset/empty behavior |
|---|---|
| `NDIF_POSTGRES_URL` | No API-key auth **and** every request is `trusted`: user code runs in-process in the model actor, and auto-provisioned models load with `trust_remote_code`. |
| `NDIF_LOKI_URL` | No log shipping; console only. The service is otherwise unaffected — the provider never imports `logging_loki`. |
| `NDIF_INFLUX_ENABLED=false` / no `NDIF_INFLUX_TOKEN` | No metrics. `enabled` short-circuits `connect` (`influx.py:112`); a bad token means writes are rejected and dropped. Either way the service keeps running. |
| `NDIF_MIN_NNSIGHT_VERSION` / `NDIF_MIN_PYTHON_VERSION` | No client version gating at all — any nnsight version may submit. |
| `NDIF_RAY_HEAD_ADDRESS` | This node starts a Ray **head** rather than joining one. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | Presigned URLs are signed with the internal endpoint — clients outside the network can't download results. |
| `NDIF_OBJECT_STORE_URL` | boto3 talks to **real AWS S3** for the configured region, not to a local MinIO. |
| `NDIF_OBJECT_STORE_ACCESS_KEY` **and** `_SECRET_KEY` (both empty) | No credentials are handed to boto3, so it walks its own chain — environment, shared config, container role, instance role. On AWS this authenticates as the role the host already carries, which is what lets a deployment hold no long-lived key at all. The keys are used only when **both** are set (`_make_client`); a half-configured pair falls back to the chain too, rather than signing with a missing half. |

## Minimum viable config

If you are standing up your own NDIF rather than running the dev compose, this is
the set that has to change; everything else has a workable default. Check the
result with `ndif doctor`, which probes Redis, the object store, the API and Ray
from these same variables (`src/ndif/cli/commands/doctor.py:85-88`).

**On every service** — Redis is the shared bus; telemetry labels separate deployments:

```bash
NDIF_REDIS_URL=redis://redis.internal:6379
NDIF_ENVIRONMENT=prod
NDIF_LOKI_URL=http://loki.internal:3100/loki/api/v1/push   # optional but cheap
NDIF_INFLUX_URL=http://influx.internal:8086                # optional but cheap
NDIF_INFLUX_TOKEN=...
```

**API service** — `NDIF_POSTGRES_URL` (auth *and* sandboxing) matters most; a wrong
`PUBLIC_URL` produces presigned links your users cannot fetch:

```bash
NDIF_RAY_ADDRESS=ray://ray-head.internal:10001
NDIF_POSTGRES_URL=postgresql://ndifapi:<pw>@postgres.internal:5432/ndif
NDIF_OBJECT_STORE_URL=https://s3.internal:9000        # what the server uploads to
NDIF_OBJECT_STORE_PUBLIC_URL=https://results.example.org  # what the client downloads from
NDIF_OBJECT_STORE_ACCESS_KEY=...
NDIF_OBJECT_STORE_SECRET_KEY=...
NDIF_MIN_NNSIGHT_VERSION=0.5.0                        # reject clients you can't serve
```

**Ray head** — object-store config must match the API's; the model actor is what
uploads results and signs the URL:

```bash
NDIF_REDIS_URL=redis://redis.internal:6379   # never leave this at localhost
NDIF_OBJECT_STORE_URL=https://s3.internal:9000
NDIF_OBJECT_STORE_PUBLIC_URL=https://results.example.org
NDIF_OBJECT_STORE_ACCESS_KEY=...
NDIF_OBJECT_STORE_SECRET_KEY=...
NDIF_DEFAULT_MODEL_ACTOR_CLASS=ndif.services.ray.sandbox.model.SandboxModelActor
NDIF_RAY_TEMP_DIR=/var/lib/ray            # if /tmp is small or tmpfs-backed
```

**Ray worker** — everything the head has, plus the join address; the controller
only runs on the node advertising the `head` resource:

```bash
NDIF_RAY_HEAD_ADDRESS=ray-head.internal:6385
```

**Dashboard**, if you run it — its defaults are the dangerous ones: `DEV_MODE` plus
a placeholder session secret means no authentication at all:

```bash
NDIF_DASHBOARD_USERNAME=...
NDIF_DASHBOARD_PASSWORD_HASH=$2b$12$...   # python -m ndif.services.dashboard.backend.auth hash <pw>
NDIF_DASHBOARD_SESSION_SECRET=<32+ random bytes>
NDIF_DASHBOARD_MONITOR_URL=https://api.example.org  # public URL, so the probe covers DNS+TLS
NDIF_API_KEY=<a real client key>          # the monitor cron's traces need one
# and do NOT set NDIF_DASHBOARD_DEV_MODE
```

## Non-NDIF environment the code reads

| Variable | Read by | Effect |
|---|---|---|
| `RAY_ADDRESS` | `src/ndif/services/ray/start.sh:54` | Ray's own variable. The head path explicitly `unset`s it so `ray start --head` doesn't try to attach to an existing cluster instead of creating one. |
| `RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES` | set to `1` in `cluster/deployment.py:177` | Stops Ray from masking GPUs from a model actor; GPU targeting is done through `max_memory` in the actor instead. |
| `PYTORCH_CUDA_ALLOC_CONF` | set to `expandable_segments:True` in `cluster/deployment.py:179` | Reduces CUDA memory fragmentation in the actor process. |
| `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` | written by `src/ndif/cli/service.py:48-49`; set in `docker-compose.yml:121-122` | MinIO reads its root credentials from these, not from `NDIF_*`; the CLI mirrors the object-store keys into them. |
| `HF_TOKEN` | host passthrough on the `ray` service (`docker/docker-compose.yml:232`), and passed through in `dashboard/start.sh:65-66` | Hugging Face auth. Compose forwards the host's `HF_TOKEN` into the `ray` service so gated repos (Llama, etc.) load, and the dashboard forwards it into the reconcile cron for the same reason. Read by `transformers` / `huggingface_hub`, not by NDIF code. Compose also bind-mounts the host HF cache (`${HOME}/.cache/huggingface` → `/root/.cache/huggingface`, `docker-compose.yml:243`) so downloaded weights persist across container restarts. |
| `HF_HOME` | passed through in `dashboard/start.sh:65-66` | Hugging Face cache location. Read by `transformers` / `huggingface_hub`. The compose `ray` service leaves it at the default `/root/.cache/huggingface`, which is the bind-mount target above. |
| `PYTHONPATH` | extended in `src/ndif/services/ray/sandbox/host.py:83` | The sandbox runner is spawned with the package root prepended so it can import `ndif.services.ray.sandbox.runner`. |
| `DOCKER_INFLUXDB_INIT_*`, `POSTGRES_*`, `GF_*` | `docker/docker-compose.yml:41-46, 101-104, 75-85` | Container-image bootstrap variables for InfluxDB, Postgres and Grafana. They must stay consistent with the `NDIF_INFLUX_*` and `NDIF_POSTGRES_URL` the services use. |

## Related

- `docs/operating/configuration.md` — the config model in prose, with the
  layering worked through on real examples.
- `docs/reference/ports.md` — which variables move a port, and what the dev
  compose exposes to the host.
- `docs/operating/compose-stack.md` — the per-container `environment:` blocks.
- `docs/operating/production.md` — what to change when you leave the dev compose.
- `docs/developing/providers.md` — how `CONFIG` specs become class attributes and
  why the providers fail open.
- `docs/concepts/auth-and-limits.md` — what `NDIF_POSTGRES_URL` and the
  `NDIF_MIN_*` gates mean for a user.
