---
title: The Compose Stack
one_liner: Every container in docker/docker-compose.yml — image, purpose, ports, volumes, health, and what breaks without it.
tags: [operating, api, ray, dashboard, redis, telemetry]
related: [docs/operating/quickstart.md, docs/operating/configuration.md, docs/operating/production.md, docs/operating/observability.md, docs/operating/cli.md, docs/concepts/services-and-topology.md, docs/reference/ports.md, docs/reference/env-vars.md, docs/gotchas/networking-and-compose.md, docs/developing/ray-service.md]
sources: [docker/docker-compose.yml, docker/Dockerfile, justfile, docker/prometheus/prometheus.yml, docker/postgres/init.sql, src/ndif/cli/service.py, src/ndif/services/ray/start.sh]
---

# The Compose Stack

## What this covers

`docker/docker-compose.yml` — ten containers, three of which are NDIF code and
seven of which are off-the-shelf infrastructure. This page is the service-by-
service reading: what each one is, what it costs you to drop it, and the four
compose-level decisions (one image, project name `dev`, the GPU reservation,
`shm_size`) that aren't obvious from the file.

The stack is a **development** stack. It publishes almost everything to
`localhost` with default credentials and no TLS. `docs/operating/production.md`
covers what has to change.

## The services

| Service | Image / build | Required? | Host ports | What breaks without it |
|---|---|---|---|---|
| `redis` | `redis:7-alpine` | **yes** | 6379 | Everything. Queue, response pub/sub, status caches, the `ray:connected` flag. |
| `minio` | `minio/minio` | **yes** | 9000, 9001 | Results have nowhere to go; the API blocks on it at startup (`depends_on: service_healthy`). |
| `api` | built from `docker/Dockerfile` | **yes** | 8001 | No client entry point. |
| `ray` | built from `docker/Dockerfile` | **yes** | 8265, 10001 | No compute. `/request`, `/status`, `/env` all 503. |
| `postgres` | `postgres:16-alpine` | health-gated | 5432 | The API won't start (it waits on the health check) — but auth stays off regardless unless you set `NDIF_POSTGRES_URL`. |
| `influxdb` | `influxdb:2.7` | health-gated | 8086 | Same: `api` and `ray` wait on its health check, then metrics are dropped silently. |
| `loki` | `grafana/loki:3.0.0` | optional | 3100 | Logs stay on stdout; `just logs` still works. |
| `prometheus` | `prom/prometheus:v2.53.0` | optional | 9090 | No Ray infrastructure metrics in Grafana. |
| `grafana` | `grafana/grafana:11.1.0` | optional | 3000 | No dashboards; the data is still in Loki/Influx/Prometheus. |
| `dashboard` | built from `docker/Dockerfile` | optional | 8081 | No admin UI, no monitor/reconcile crons. Deploys still work via the CLI. |

"Required" means the request path cannot function. "Health-gated" is a compose
artifact: `api` declares `depends_on: {postgres: service_healthy, influxdb:
service_healthy}` (`docker-compose.yml:159-167`), so those two containers must
reach healthy before the API starts, whether or not the API will use them.

### redis (`docker-compose.yml:11`)

The central nervous system. The API pushes requests onto a Redis list, the
dispatcher pops them, model actors publish status/response JSON onto per-session
pub/sub channels, cached `/status` and `/env` blobs live here, and the
`ray:connected` flag gates the API's routes. Health check: `redis-cli ping`.

> **Gotcha:** `NDIF_REDIS_URL` must be set explicitly on the `ray` service
> (`docker-compose.yml:213`). Its default is `redis://localhost:6379` — and
> inside the `ray` container, port 6379 is Ray's *own* GCS default, not Redis.
> Model actors would connect to the wrong server and the response handshake
> would fail. See `docs/gotchas/networking-and-compose.md`.

### minio (`docker-compose.yml:117`)

S3-compatible object store for result blobs. Results are far too large for the
Redis pub/sub channel, so the model actor uploads them and publishes a presigned
GET URL on the `COMPLETED` response; the client downloads directly. Two ports:
9000 is the S3 API, 9001 the web console. Credentials are `minioadmin` /
`minioadmin` on both sides.

This is where the two-endpoint split matters. `NDIF_OBJECT_STORE_URL` is
`http://minio:9000` (what the server uploads through) while
`NDIF_OBJECT_STORE_PUBLIC_URL` is `http://localhost:9000` (what the URL is
*signed* with, because your client is on the host). Both are set on `api` and
`ray` (`docker-compose.yml:150-151`, `223-224`).

### postgres (`docker-compose.yml:98`)

The users/keys database for API-key auth. `docker/postgres/init.sql` runs once on
an empty data dir and creates the full production schema — `users`, `profiles`,
`keys`, `user_tags`, `key_user_tag_assignments`, `models`, `audit_logs` — plus
the read-only `ndifapi` role the API connects as and the `login_page` role the
account portal uses (`init.sql:161-171`).

**No users or keys are seeded, and `NDIF_POSTGRES_URL` is commented out**
(`docker-compose.yml:156`). Auth is therefore off, and with auth off a
request is `trusted` unless it says otherwise (`src/ndif/services/api/auth.py:180`). Uncommenting
that one line is the switch; `docs/runbooks/enable-auth.md` walks the rest.

### influxdb / loki / prometheus / grafana (`docker-compose.yml:38, 23, 59, 72`)

The telemetry tier, all fail-open.

- **InfluxDB** stores numeric time series. It boots in `setup` mode with a
  hardcoded org/bucket/token (`ndif` / `metrics` / `ndif-dev-token`) that the
  `NDIF_INFLUX_*` env on `api` and `ray` matches, so it works with no manual step.
- **Loki** takes shipped log lines. Services push to it only when
  `NDIF_LOKI_URL` is set — which compose does for `api` and `ray`, not for
  `dashboard`.
- **Prometheus** scrapes exactly one target, `ray:8080`
  (`docker/prometheus/prometheus.yml:20`) — Ray's metrics-export port, set by
  `--metrics-export-port` in `services/ray/start.sh:66`. Ray is the only thing in
  the stack that speaks Prometheus. There is deliberately no `depends_on: ray`;
  Prometheus starts and retries until the slow GPU container is up.
- **Grafana** runs with anonymous admin and the login form disabled, with the
  Loki/Influx/Prometheus/Postgres datasources provisioned from
  `docker/grafana/provisioning`. It lands on the NDIF Overview dashboard.

Drop all four and NDIF runs identically — you just lose visibility. See
`docs/operating/observability.md`.

### api (`docker-compose.yml:132`)

`NDIF_SERVICE=api` → `ndif start api` → `api/start.sh` → gunicorn with uvicorn
workers serving `ndif.services.api.app:app`, plus the queue dispatcher spawned
once in the master. Publishes 8001, the only port a client ever touches.

### ray (`docker-compose.yml:204`)

`NDIF_SERVICE=ray` → `ray/start.sh`. Because `NDIF_RAY_HEAD_ADDRESS` is unset it
starts a Ray **head** and then launches the NDIF controller
(`start.sh:50-70`). Model deployments are plain detached Ray actors placed by the
controller (`cluster/deployment.py:192-196`), looked up by name in the `NDIF`
namespace — there is no Ray Serve involved anywhere in this repo.

Publishes 8265 (Ray dashboard) and 10001 (the `ray://` client server the API and
CLI connect through). Its Prometheus port 8080 and GCS port stay internal.

The `ray` service bind-mounts the host Hugging Face cache
(`${HOME}/.cache/huggingface` → `/root/.cache/huggingface`) and passes `HF_TOKEN`
through (`HF_TOKEN: ${HF_TOKEN:-}`), so downloaded weights persist across
`just down` and gated repos resolve when the token is in your environment.

> **Gotcha:** compose sets
> `NDIF_DEFAULT_MODEL_ACTOR_CLASS=ndif.services.ray.sandbox.model.SandboxModelActor`
> (`docker-compose.yml:228`), but the code default is the in-process
> `ndif.services.ray.deployments.modeling.base.ModelActor`. A bare `pip install`
> plus `ndif start` therefore does **not** run the same execution path as
> `just up`. If you reproduce a compose behavior outside compose, set this
> variable.

### dashboard (`docker-compose.yml:173`)

Same image, `NDIF_SERVICE=dashboard` → uvicorn serving the Vue UI and its FastAPI
backend on 8081, plus a cron daemon running the monitor and reconcile jobs
(`dashboard/start.sh:48-74`). It reaches the API, Ray and Redis by compose
service name.

`NDIF_DASHBOARD_DEV_MODE: "true"` (`docker-compose.yml:192`) makes
`require_auth` return the configured username without checking anything
(`dashboard/backend/auth.py:73`) — the UI opens with no login. The compose
comment spells out the production alternative: drop dev mode, set
`NDIF_DASHBOARD_USERNAME`, a bcrypt `NDIF_DASHBOARD_PASSWORD_HASH`, and a random
`NDIF_DASHBOARD_SESSION_SECRET`. See `docs/operating/dashboard.md`.

## Four decisions in the file

### One image, `NDIF_SERVICE` picks the role

`api`, `ray` and `dashboard` are the same build (`context: ..`, `docker/Dockerfile`).
The image's entrypoint is `ndif start --foreground` (`Dockerfile:49`), and with no
argument `ndif start` resolves its targets from `$NDIF_SERVICE`
(`env_services`, `src/ndif/cli/service.py:83-85`). A single target is `exec`'d so it becomes PID 1
and gets signals directly (`cli/commands/start.py:59-66`).

This is why one heavy layer (`requirements.txt`: torch cu124, Ray, transformers,
nnsight) is shared across all three containers, and why the compose
`environment:` blocks are the entire difference between them. `NDIF_SERVICE` accepts a space/comma list,
so one container can supervise several services.

The image installs `ndif` with the `api,ray,metrics,postgres,dashboard` extras and
`--no-deps`, since `requirements.txt` already pins everything (`Dockerfile:45`).

### `name: dev`

`docker-compose.yml:8` sets the compose project name, so containers and networks
are `dev-api-1`, `dev-ray-1`, `dev_default` — rather than being named after the
`docker/` directory the file lives in. Useful to know when you reach past `just`
for `docker logs dev-ray-1` or `docker exec`.

### The GPU reservation

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

(`docker-compose.yml:257-263`) — the compose equivalent of `docker run --gpus
all`. It requires the NVIDIA container toolkit on the host; without it the `ray`
container fails to create. Only `ray` gets GPUs; nothing else needs one.

### `shm_size: "4gb"`

Ray's plasma object store lives in `/dev/shm`, and Docker's default shared-memory
size is **64 MB** — far too small (`docker-compose.yml:254-256`). Ray will either
spill to disk and crawl or fail outright. 4 GB is a development floor; size it to
your real object traffic in production.

## `just` is the interface

Every recipe is a thin wrapper over `docker compose -f docker/docker-compose.yml`
(`justfile:14`).

| Recipe | Does | When |
|---|---|---|
| `just` | Lists the recipes | — |
| `just up [services...]` | `compose up -d` | Start everything, or a subset: `just up api redis` |
| `just down [args...]` | `compose down` | Stop and remove. `just down -v` also drops volumes |
| `just build [services...]` | `compose build` | Rebuild the image after a code change |
| `just ta [services...]` | `down` → `build` → `up` | **The full refresh.** Use after any change to `src/` |
| `just restart [services...]` | `compose restart` | Bounce a container to re-read nothing but its own state |
| `just logs [services...]` | `compose logs -f` | Follow; Ctrl-C detaches without stopping anything |
| `just ps` | `compose ps` | Container status and health |

`just ta` exists because the source is **baked into the image** — there is no bind
mount of `src/` — so `just restart` or `just up` after editing code runs the old
build. `just ta ray` narrows the rebuild to one service, but note that the recipe
runs a full `just down` first (`justfile:33-36`): the whole stack comes down
either way.

The one exception is nnsight. It ships in the image (it's in `requirements.txt`),
but `just up`/`just ta` also include `docker/docker-compose.nnsight.yml`, which
bind-mounts a local editable nnsight over the image's copy (resolved from
`NNSIGHT_PATH`, which the `justfile` sets by importing nnsight). Install nnsight
editable and client-side changes are picked up without a rebuild; if nnsight
isn't importable the override is skipped and the image's own copy is used.

## Persistence

Only `dashboard_data` is a named volume (`docker-compose.yml:196, 258-259`),
holding the dashboard's logs, `schedule.json`, config and cache. The `ray`
service also bind-mounts the host HF cache, so model weights persist. Everything
else is stateless or a read-only bind mount of config:

- **Result blobs, metrics, logs, and the Postgres database do not survive
  `just down`** — MinIO, InfluxDB, Loki and Postgres all write to the container
  filesystem. This is deliberate: the dev stack is disposable and keeps no
  telemetry or Redis volumes.
- Postgres losing its data dir means `init.sql` runs again on the next boot,
  recreating the schema empty. Any API keys you created are gone.
- Downloaded model weights survive `just down` — they live in the host HF cache
  the `ray` service bind-mounts (`${HOME}/.cache/huggingface`).

Add volumes for anything else you want to keep. `docs/operating/production.md`
covers which.

## Related

- `docs/operating/quickstart.md` — bringing this up for the first time.
- `docs/operating/configuration.md` — where these `environment:` blocks sit in the layering.
- `docs/reference/ports.md` — every port, published or not.
- `docs/concepts/services-and-topology.md` — what talks to what, and why.
- `docs/operating/production.md` — what to change before this faces users.
