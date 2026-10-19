---
title: Repo Layout
one_liner: Directory by directory — what lives where in the NDIF repo, and which file to open for a given kind of change.
tags: [internals, dev]
related: [docs/developing/architecture-overview.md, docs/developing/api-service.md, docs/developing/ray-service.md, docs/developing/cli-internals.md, docs/developing/dashboard-internals.md, docs/developing/providers.md, docs/developing/adding-a-service.md, docs/developing/testing.md, docs/developing/contributing.md, docs/operating/compose-stack.md, docs/reference/env-vars.md]
sources: [pyproject.toml, justfile, docker/Dockerfile, docker/docker-compose.yml, src/ndif/cli/service.py, src/ndif/common/providers/base.py, src/ndif/services/ray/sandbox/ARCHITECTURE.md, src/ndif/services/ray/tp/ARCHITECTURE.md, tests/conftest.py]
---

# Repo Layout

## What this covers

Where everything lives, and why. Two structural facts explain most of the layout:

1. **One package, one image, several services.** `src/ndif/` is a single installable
   package (`pip install ndif`). `docker/Dockerfile` builds *one* image whose
   `ENTRYPOINT` is the `ndif` CLI and whose default command is `start --foreground`;
   which services a container runs is decided at runtime by `NDIF_SERVICE` (see
   [Adding a service](adding-a-service.md#the-contract)). There is no per-service
   image or Dockerfile.
2. **Shared code sits in `common/`, service code under `services/<name>/`.**
   `common/` may not import from `services/`. Anything two services must agree on
   (Redis key names, request/response schemas, provider env vars) is in `common/` so
   it cannot drift.

## Top level

| Path | What it is |
|---|---|
| `src/ndif/` | the package: CLI, shared library, services |
| `docker/` | the Dockerfile, the dev compose stack, the nnsight bind-mount override, and provisioning for Grafana / Prometheus / Postgres |
| `tests/` | live-server, model-only and offline tests ([Testing](testing.md)) |
| `docs/` | these docs; [docs/index.md](../index.md) is the map |
| `justfile` | a thin wrapper over `docker compose` |
| `pyproject.toml` | package metadata, extras, the `ndif` console script, `package-data` |
| `requirements.txt` | the pinned dependency set the image installs as its own layer. torch is not here — the Dockerfile installs it from a build arg, since the wheel depends on the host's CUDA driver |
| `.github/workflows/` | build and publish only ([Contributing](contributing.md#ci-and-releases)) |
| `README.md`, `CONTRIBUTING.md`, `CLAUDE.md`, `AGENTS.md` | install/quickstart, contribution rules, and the agent guide |

## `src/ndif/cli/` — the `ndif` command

The console script `ndif = "ndif.cli:cli"`: a click group with one command per module.
[CLI internals](cli-internals.md) covers it in depth.

| Path | Role |
|---|---|
| `main.py` | the click group; registers every subcommand and loads `.env` first |
| `config.py` | `DEFAULTS` (single-host fallbacks) and `build_env` |
| `service.py` | the service registry: `SERVICES` (redis, rustfs, ray, api) and `OPTIONAL_SERVICES` (dashboard) |
| `state.py` | PID/log files under `$NDIF_HOME` (default `~/.ndif`) |
| `util.py` | presentation helpers and process signalling |
| `commands/*.py` | one module per verb — click decoration only |
| `lib/*.py` | the logic behind the verbs, importable without click; the dashboard backend calls it directly |

## `src/ndif/common/` — shared by every service

| Path | Role |
|---|---|
| `providers/` | one module per external service behind the `Provider` base ([Providers](providers.md)) |
| `redis/` | Redis key constants only: the `/status` and `/env` caches and the dispatcher's event stream |
| `schema/` | `BackendRequestModel` / `BackendResponseModel` (subclasses of nnsight's, so the wire format is the client's) and the controller RPC contracts |
| `errors.py` | caller-facing errors shared by the in-process and sandbox paths |
| `metrics.py`, `telemetry.py`, `logging_setup.py` | metrics classes, `event()`, the console formatter ([Telemetry internals](telemetry-internals.md)) |
| `types.py` | `MODEL_KEY`, `REPLICA_ID`, `NODE_ID` aliases |

## `src/ndif/services/api/` — FastAPI ingress and the queue

```
api/
├── start.sh            exec gunicorn with gunicorn_conf
├── gunicorn_conf.py    binds the port; forks workers; spawns the dispatcher
├── app.py              every HTTP/WS endpoint
├── auth.py             API-key verification; stamps email and tags
├── versioning.py       the client nnsight/python version gate
└── queue/
    ├── config.py         NDIF_QUEUE_* / NDIF_AUTOSCALING_* knobs
    ├── dispatcher.py     pops the Redis queue, owns one Processor per model
    ├── processor.py      per-model queue + replica pool + autoscaling loop
    ├── request_queue.py  the per-model queue: FIFO within two priority groups
    └── replica.py        one model actor; the task that dispatches to it
```

The dispatcher is not a separate container: `gunicorn_conf.py`'s `on_starting`
**spawns** it from the gunicorn master ([why spawn](telemetry-internals.md#the-api-gunicorn)).

## `src/ndif/services/ray/` — the GPU side

```
ray/
├── start.sh            ray start (head or worker), then launches the controller
├── resources.py        prints this node's custom Ray resources as JSON
├── deployments/
│   ├── controller/
│   │   ├── controller.py      the detached Controller actor
│   │   └── cluster/
│   │       ├── cluster.py     in-memory model of nodes + placement decisions
│   │       ├── node.py        one GPU node: HOT deployments and WARM cache
│   │       ├── deployment.py  one replica; actor create/delete/cache ops
│   │       └── evaluator.py   checkpoint size estimate and shardability, memoized
│   └── modeling/
│       ├── base.py     BaseModelDeployment (the run/execute template) + ModelActor
│       ├── nns.py      rebuild and run a traced block; autocast region; block_scope
│       └── util.py     dtype resolution, device maps, CPU-relocating pickler, request meta
├── sandbox/
│   ├── ARCHITECTURE.md the design of record for this subtree
│   ├── model.py        SandboxHost + SandboxModelDeployment + SandboxModelActor
│   ├── driver.py       the host side of a run: proxies, interleave, control events
│   ├── host.py         spawn runners, Pool, Connection
│   ├── runner.py       the runner process entry point
│   ├── nns.py          nnsight patches that run only inside the runner
│   └── protocol.py     framing, codec, and the message catalog
└── tp/
    ├── ARCHITECTURE.md the tensor-parallel design
    ├── model.py        TPModelActor and SandboxedTPModelActor (rank 0)
    ├── host.py         rank 0's ShardGroup: spawn and drive the other ranks
    ├── shard.py        a non-zero rank: holds a shard, runs what rank 0 runs
    └── common.py       what every rank must do identically
```

Whether a request's Python runs in the actor or in a runner is decided per request
by `request.trusted`, not by which actor class is deployed — a trusted block runs
*in* the model actor process even when the sandbox actor is deployed. Read
`src/ndif/services/ray/sandbox/ARCHITECTURE.md` before changing anything under
`sandbox/` ([Sandbox internals](sandbox-internals.md)).

The `requirements.txt` files under `services/api/` and `services/ray/` are empty;
dependencies come from the root `requirements.txt` and `pyproject.toml`.

## `src/ndif/services/dashboard/` — admin UI

```
dashboard/
├── start.sh        writes /etc/cron.d/ndif-dashboard (if cron exists), execs uvicorn
├── backend/        FastAPI: auth, schedule CRUD, monitor reads, deploy/evict
│   └── ndif_client.py   thin wrapper over cli/lib — the dashboard's only door in
├── jobs/           cron entry points: monitor.py, reconcile.py, report.py
├── frontend/       Vue 3 + Vite SPA; frontend/dist/ is the committed build
└── config.example.json
```

[Dashboard internals](dashboard-internals.md) covers the backend and jobs;
[Dashboard frontend](dashboard-frontend.md#build-and-packaging) how the committed
build ships.

## `docker/`

| Path | What it is |
|---|---|
| `Dockerfile` | one image for all services: `python:3.12-slim`, torch from a `--index-url` build arg (`TORCH_CUDA`), `requirements.txt` in its own layer, then the package with `--no-deps` and the `ext` extra's list; `redis-server` and a rustfs binary so `NDIF_SERVICE=all` runs them in-container; `ndif version --write /etc/ndif/build.json` records what resolved |
| `docker-compose.yml` | the dev stack: redis, loki, influxdb, prometheus, grafana, postgres, rustfs, api, dashboard, ray — every NDIF service shares one `build:` block and differs by env |
| `docker-compose.nnsight.yml` | bind-mounts an editable nnsight over the image's copy ([how](nnsight-integration.md#developing-against-a-local-nnsight)) |
| `grafana/provisioning/` | datasources and the pre-built NDIF dashboards |
| `prometheus/prometheus.yml` | scrapes Ray's metrics port; Ray is the only Prometheus speaker |
| `postgres/init.sql` | the users/keys schema and a read-only role; no seed data |

## `justfile`

Wraps `docker compose -f docker/docker-compose.yml`: `just build`, `just up
[service...]`, `just down [args]`, `just ta` (down → build → up), `just restart`,
`just logs`, `just ps`, and `just nnsight` (what nnsight would be bind-mounted). It
also computes `NDIF_VERSION` for the build and adds the nnsight override when an
editable nnsight is found. `just ta <svc>` tears the **whole** stack down and brings back only that service and its dependencies; HOT deployments are lost.

## `pyproject.toml`

- **Extras** gate optional subsystems: `api`, `ray`, `metrics`, `postgres`,
  `dashboard`, `dev`, and `ext` (packages user blocks may import). Adding a
  dependency or shipping a non-Python file is
  [Adding a service](adding-a-service.md#dependencies-extras-and-the-image).
- **The version is the git tag** (setuptools-scm, `dynamic = ["version"]`).
- **nnsight is pinned to the 0.8 line** — see
  [nnsight integration](nnsight-integration.md#what-this-covers) for why the
  specifier names the pre-release.

## I want to change X, open Y

| I want to change... | Open |
|---|---|
| An HTTP endpoint, or what a request looks like at ingress | `src/ndif/services/api/app.py` |
| Who is allowed in, or how `trusted` is decided | `src/ndif/services/api/auth.py` |
| Queue behavior, priority, autoscaling triggers | `src/ndif/services/api/queue/` |
| Where replicas get placed, or eviction policy | `src/ndif/services/ray/deployments/controller/cluster/cluster.py`, `node.py` |
| Size estimation and tensor-parallel shardability | `src/ndif/services/ray/deployments/controller/cluster/evaluator.py` |
| Ray actor lifecycle (create / delete / HOT↔WARM) | `src/ndif/services/ray/deployments/controller/cluster/deployment.py` |
| How a model loads, or the per-request run template | `src/ndif/services/ray/deployments/modeling/base.py` |
| How a traced block is rebuilt and run | `src/ndif/services/ray/deployments/modeling/nns.py` |
| The trusted/untrusted execution fork | `src/ndif/services/ray/sandbox/model.py` |
| The sandbox wire protocol | `src/ndif/services/ray/sandbox/protocol.py` (+ `ARCHITECTURE.md`) |
| Tensor-parallel replicas | `src/ndif/services/ray/tp/` (+ `ARCHITECTURE.md`) |
| An `ndif` subcommand's flags / what it does | `src/ndif/cli/commands/<verb>.py` / `src/ndif/cli/lib/<verb>.py` |
| Which services `ndif start` knows about | `src/ndif/cli/service.py` |
| A default port or URL for single-host runs | `src/ndif/cli/config.py` (`DEFAULTS`) |
| A connection to an external system | `src/ndif/common/providers/<name>.py` + a pyproject extra |
| A Redis key, channel, or stream name | `src/ndif/common/redis/` |
| What a log line or metric carries | `src/ndif/common/telemetry.py`, `src/ndif/common/metrics.py` |
| The dev stack's topology, ports, or env | `docker/docker-compose.yml` |
| What's installed in the image | `requirements.txt` (pins) + `pyproject.toml` (extras) |
| A Grafana dashboard | `docker/grafana/provisioning/dashboards/json/` |
| The admin UI | `src/ndif/services/dashboard/backend/routers/`, then rebuild `frontend/` |

## Related

[Architecture overview](architecture-overview.md) for the same system top-down;
[Compose stack](../operating/compose-stack.md) for the `docker/` tree from an
operator's angle.
