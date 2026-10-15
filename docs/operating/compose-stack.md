---
title: The Compose Stack
one_liner: Every container in docker/docker-compose.yml — image, purpose, ports, persistence, health, the service-name wiring between them, and what breaks without each.
tags: [operating, api, ray, dashboard, redis, telemetry]
related: [docs/operating/quickstart.md, docs/operating/configuration.md, docs/operating/production.md, docs/operating/observability.md, docs/operating/dashboard.md, docs/operating/cli.md, docs/concepts/services-and-topology.md, docs/reference/ports.md, docs/reference/env-vars.md, docs/developing/ray-service.md]
sources: [docker/docker-compose.yml, docker/docker-compose.nnsight.yml, docker/Dockerfile, justfile, docker/prometheus/prometheus.yml, docker/postgres/init.sql, src/ndif/cli/service.py, src/ndif/services/ray/start.sh, src/ndif/services/ray/deployments/controller/cluster/deployment.py]
---

# The Compose Stack

## What this covers

`docker/docker-compose.yml` — ten containers, three of which are NDIF code and
seven off-the-shelf infrastructure: what each one is, what it costs you to drop
it, how they find each other, and what survives `just down`.

It is a **development** stack: it builds the image from this checkout and
publishes almost everything on every interface with default credentials and no
TLS. To just *run* NDIF, `docker run --gpus all ndif/ndif` starts redis, minio,
ray and the API in one container from the published image instead — see
[Quickstart](quickstart.md). [Going to Production](production.md) covers what
has to change before either faces users.

## The services

| Service | Image / build | Required? | Host ports | What breaks without it |
|---|---|---|---|---|
| `redis` | `redis:7-alpine` | **yes** | 6379 | Everything. Queue, response pub/sub, status caches, the `ray:connected` flag. |
| `minio` | `quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z` | **yes** | 9000, 9001 | Large results have nowhere to go; `api` and `ray` wait on its health check. |
| `api` | built from `docker/Dockerfile` | **yes** | 8001 | No client entry point. |
| `ray` | built from `docker/Dockerfile` | **yes** | 8265, 10001 | No compute. `/request`, `/status`, `/env` all 503. |
| `postgres` | `postgres:16-alpine` | health-gated | 5432 | `api` won't start (it waits on the health check) — but auth stays off regardless unless you set `NDIF_POSTGRES_URL`. |
| `influxdb` | `influxdb:2.7` | health-gated | 8086 | Same: `api` and `ray` wait on its health check, then metrics are dropped silently. |
| `loki` | `grafana/loki:3.0.0` | optional | 3100 | Logs stay on stdout; `just logs` still works. |
| `prometheus` | `prom/prometheus:v2.53.0` | optional | 9090 | No Ray infrastructure metrics in Grafana. |
| `grafana` | `grafana/grafana:11.1.0` | optional | 3000 | No dashboards; the data is still in Loki/Influx/Prometheus. |
| `dashboard` | built from `docker/Dockerfile` | optional | 8081 | No admin UI, no monitor/reconcile crons. Deploys still work via the CLI. |

A host port already in use fails that service's start; change its `ports:`
entry in `docker/docker-compose.yml`. The justfile passes `-f` explicitly, so
compose does not read a `docker-compose.override.yml`.

"Health-gated" is a compose artifact: the `api` service's `depends_on` waits for
`redis`, `minio`, `influxdb` and `postgres` to be `service_healthy`, whether or
not the API will use them. `api` has its own health check — `GET /ping` through
python, since the image has no curl.

- **redis** — the API pushes requests onto a Redis list, the dispatcher pops
  them, model actors publish status and responses on per-session pub/sub
  channels, and cached `/status` and `/env` blobs live here.
- **minio** — S3-compatible store for result blobs too large to inline; the
  model actor uploads them and publishes a presigned GET URL the client
  downloads directly. 9000 is the S3 API, 9001 the web console. Pulled from
  quay.io and pinned because `minio/minio` on Docker Hub no longer resolves; the
  image copies its own `minio` binary from the same release.
- **postgres** — the users/keys database for API-key auth.
  `docker/postgres/init.sql` runs once on an empty data dir and creates the
  schema plus the read-only `ndifapi` role the API connects as. No users or keys
  are seeded and `NDIF_POSTGRES_URL` is commented out on `api`, so auth is off;
  [Enable auth](../runbooks/enable-auth.md) walks turning it on.
- **influxdb / loki / prometheus / grafana** — the telemetry tier, all
  fail-open. InfluxDB boots in `setup` mode with an org/bucket/token
  (`ndif` / `metrics` / `ndif-dev-token`) that the `NDIF_INFLUX_*` env on `api`
  and `ray` matches. Prometheus scrapes exactly one target, `ray:8080` (Ray's
  `--metrics-export-port`), and has no `depends_on: ray`, so it retries until
  Ray is up. Grafana runs with anonymous admin and provisioned datasources. Drop
  all four and NDIF runs identically; see [Observability](observability.md).
- **api** — `NDIF_SERVICE=api` → `ndif start api` → gunicorn with uvicorn
  workers serving `ndif.services.api.app:app`, plus the queue dispatcher spawned
  once by the master.
- **ray** — `NDIF_SERVICE=ray` → `src/ndif/services/ray/start.sh`. With
  `NDIF_RAY_HEAD_ADDRESS` unset it starts a Ray **head** and launches the NDIF
  controller. Model replicas are detached Ray actors in the `NDIF` namespace —
  no Ray Serve. Publishes 8265 (Ray dashboard) and 10001 (the `ray://` client
  server the API and CLI connect through); its metrics port 8080 and GCS port
  stay internal. It sets `NDIF_DEFAULT_MODEL_ACTOR_CLASS` to the sandbox actor,
  which the image and a from-source install do not — see
  [Configuration](configuration.md#what-must-differ-from-the-dev-defaults).
- **dashboard** — same image, `NDIF_SERVICE=dashboard` → uvicorn on 8081 plus a
  cron daemon running the monitor, reconcile and report jobs. Compose turns its
  login off with `NDIF_DASHBOARD_DEV_MODE: "true"`; see
  [Admin Dashboard](dashboard.md).

## Four decisions in the file

### One image, `NDIF_SERVICE` picks the role

`api`, `ray` and `dashboard` are the same build. The image's entrypoint is the
`ndif` CLI with `start --foreground` as its command, and with no argument
`ndif start` resolves its targets from `$NDIF_SERVICE` (`src/ndif/cli/service.py`
`env_services`), which the image defaults to `all`. A single target is `exec`'d
so it becomes PID 1 and gets signals directly. `NDIF_SERVICE` accepts a
space/comma list and `resolve_targets` expands `all` in place, so
`"all dashboard"` is the core stack plus the admin UI.

That is why the heavy layers — torch, then `requirements.txt` — are shared
across all three containers, and why the compose `environment:` blocks are the
entire difference between them. The image also carries `redis-server` and a
`minio` binary that only `NDIF_SERVICE=all` uses; compose runs its own `redis`
and `minio` containers instead.

### `name: dev`

The compose project name is `dev`, so containers and the network are
`dev-api-1`, `dev-ray-1`, `dev_default` — useful when you reach past `just` for
`docker logs dev-ray-1` or `docker exec`. The name is fixed, so every `just up`
on a host reuses project `dev`: an existing `dev` stack is taken over, and
existing `dev-*` images are retagged by the next build.

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

The compose equivalent of `docker run --gpus all`, on the `ray` service only. It
requires the NVIDIA container toolkit on the host; without it the `ray`
container fails to create.

### `shm_size: "4gb"`

Ray's plasma object store lives in `/dev/shm`, and Docker's default shared-memory
size is **64 MB**. Ray then spills to disk and crawls or fails outright, and
neither failure mentions `shm_size`. 4 GB is a development floor. Running the
`ray` service anywhere else needs the same setting — `docker run
--shm-size=4gb`, a Kubernetes `emptyDir` with `medium: Memory`, or your
orchestrator's equivalent.

## Service names, not localhost

Every provider defaults to `localhost`, which is right for a single-host
`ndif start` and wrong on the compose network. Compose overrides them **per
service**; a service that misses one silently talks to itself.

| Variable | Provider default | Compose sets | On |
|---|---|---|---|
| `NDIF_REDIS_URL` | `redis://localhost:6379` | `redis://redis:6379` | `api`, `ray`, `dashboard` |
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | `ray://ray:10001` | `api`, `dashboard` — not `ray`, which starts the cluster rather than dialing it |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | `http://minio:9000` | `api`, `ray` |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | falls back to the above | `http://localhost:9000` | `api`, `ray` |
| `NDIF_LOKI_URL` | unset (shipping off) | `http://loki:3100/loki/api/v1/push` | `api`, `ray` |
| `NDIF_INFLUX_URL` | unset (metrics off) | `http://influxdb:8086` | `api`, `ray`; `dashboard` only reads it, for its report cron |
| `NDIF_API_URL` | `http://localhost:8001` | `http://api:8001` | `dashboard` (its report cron) |
| `NDIF_POSTGRES_URL` | empty (auth off) | commented out | — |

`NDIF_OBJECT_STORE_PUBLIC_URL` is the one URL about someone else's network: it
signs result URLs for the *client*, so `localhost:9000` works only for a client
on this host. A colleague on your LAN needs an address they can resolve.

The costly omission is `NDIF_REDIS_URL` on `ray`. It is not a startup crash: the
controller and model actors come up, a request runs to completion on the GPU,
and then the response cannot be published — the client sits at `DISPATCHED` or
`RUNNING` while the job is already done. Model actors inherit the controller's
provider config through their Ray `runtime_env`
(`src/ndif/services/ray/deployments/controller/cluster/deployment.py`
`_provider_runtime_env`), so setting it on `ray` fixes every actor, and missing
it breaks every actor at once.

Reproducing compose outside compose means reproducing this table, plus the
actor class.

```bash
# What did each service actually resolve its providers to?
docker compose -f docker/docker-compose.yml exec ray env | grep '^NDIF_' | sort

# Is Redis reachable from the ray container by service name?
docker compose -f docker/docker-compose.yml exec ray \
  python -c "import redis,os; print(redis.from_url(os.environ['NDIF_REDIS_URL']).ping())"
```

## `just` is the interface

Every recipe is a thin wrapper over `docker compose -f docker/docker-compose.yml`
(`just --list` shows them).

| Recipe | Does | When |
|---|---|---|
| `just up [services...]` | `compose up -d` | Start everything, or a subset. Also recreates a container whose `environment:` changed |
| `just down [args...]` | `compose down` | Stop and remove. `just down -v` also drops volumes |
| `just build [services...]` | `compose build` | Rebuild the image |
| `just ta [services...]` | `just down` → `build` → `up` | **The full refresh** after any change to `src/` |
| `just restart [services...]` | `compose restart` | Restart processes with their *existing* environment — not a way to apply config |
| `just logs [services...]` | `compose logs -f` | Follow; Ctrl-C detaches without stopping anything |
| `just ps` | `compose ps` | Container status and health |
| `just nnsight` | — | Which nnsight, if any, would be bind-mounted |

`just ta` exists because the source is **baked into the image** — there is no
bind mount of `src/` — so `just restart` or `just up` after editing code runs the
old build. The `ta` recipe always runs a bare `just down` first, so
`just ta ray` takes the **whole stack** down, rebuilds `ray`, and starts only
`ray` and its dependencies; follow it with `just up` to bring the rest back.

**nnsight is the exception.** It ships in the image (`nnsight>=0.8.0rc1,<0.9`
in `requirements.txt`), but when the shell you run `just` from has an
**editable** nnsight installed (`pip install -e /path/to/nnsight`), or
`NNSIGHT_PATH` is set, `just up`/`just ta` add
`docker/docker-compose.nnsight.yml`, which bind-mounts that checkout over the
image's copy in `api`, `ray` and `dashboard`. Client-side changes are then
picked up without a rebuild. A non-editable nnsight under site-packages is
deliberately **not** mounted — it would replace the image's pinned copy with
whatever your shell has — and no nnsight at all skips the override.

## Persistence

| Survives `just down` | Lost on `just down` |
|---|---|
| Model weights — the `ray` service bind-mounts `${HOME}/.cache/huggingface` | Result blobs (MinIO writes to the container filesystem) |
| Dashboard state — the named volume `dashboard_data` (logs, `schedule.json`, config, cache); `just down -v` drops it | The Postgres database: `init.sql` runs again on the next boot and recreates the schema empty, so **every API key you created is gone** |
| | Metrics, logs and UI-edited Grafana panels (InfluxDB, Loki, Prometheus, Grafana) |

The dev stack is disposable by design. Add volumes for anything you want to
keep; [Going to Production](production.md) lists which.

## Related

- [Quickstart](quickstart.md) — bringing this up for the first time.
- [Configuration](configuration.md) — where these `environment:` blocks sit in the precedence.
- [Ports](../reference/ports.md) — every port, published or not.
- [Services and Topology](../concepts/services-and-topology.md) — what talks to what, and why.
- [Going to Production](production.md) — what to change before this faces users.
