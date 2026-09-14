---
title: Repo Layout
one_liner: Directory by directory — what lives where in the NDIF repo, and which file to open for a given kind of change.
tags: [internals, dev]
related: [docs/developing/architecture-overview.md, docs/developing/api-service.md, docs/developing/ray-service.md, docs/developing/cli-internals.md, docs/developing/dashboard-internals.md, docs/developing/providers.md, docs/developing/adding-a-service.md, docs/developing/testing.md, docs/developing/contributing.md, docs/operating/compose-stack.md, docs/reference/env-vars.md]
sources: [pyproject.toml, justfile, docker/Dockerfile, docker/docker-compose.yml, src/ndif/cli/service.py, src/ndif/common/providers/base.py, src/ndif/services/ray/sandbox/ARCHITECTURE.md, tests/conftest.py]
---

# Repo Layout

## What this covers

Where everything lives, and why it lives there. Two structural facts explain most
of the layout:

1. **One package, one image, three services.** `src/ndif/` is a single installable
   package (`pip install ndif`). `docker/Dockerfile` builds *one* image whose
   `ENTRYPOINT` is the `ndif` CLI and whose default command is
   `start --foreground` (`docker/Dockerfile:138`-`139`); which service a container
   runs is decided at runtime by `NDIF_SERVICE` — one name, a space/comma list, or
   the image's default `all`, which expands to redis, minio, ray and api
   (`cli/service.py:88`). `ndif start` resolves each name to a launcher: a
   service's own `start.sh` for `api` / `ray` / `dashboard`, the server binary for
   `redis` / `minio`. There is no per-service image, Dockerfile, or requirements
   file.
2. **Everything shared sits in `common/`, everything service-specific sits under
   `services/<name>/`.** The rule that keeps it honest: `common/` may not import
   from `services/`. Config that two services must agree on (Redis key names,
   request/response schemas, provider env vars) is in `common/` precisely so they
   cannot drift.

## Top level

| Path | What it is |
|---|---|
| `src/ndif/` | The whole package: CLI, shared library, three services. |
| `docker/` | The single Dockerfile, the dev compose stack, and provisioning for Grafana / Prometheus / Postgres. |
| `tests/` | One live-server pytest suite (see [testing.md](./testing.md)). |
| `docs/` | These docs. |
| `justfile` | Thin wrapper over `docker compose -f docker/docker-compose.yml`. |
| `pyproject.toml` | Package metadata, extras, and the `ndif` console script. |
| `requirements.txt` | The pinned dependency set (nnsight, ray, fastapi, redis, boto3, ...) installed as its own Docker layer so a source change doesn't redownload multiple GB. torch is *not* here — `docker/Dockerfile` installs it from a build arg, since the right wheel depends on the host's CUDA driver. `transformers>=5.15.0` is a floor, not a pin. |
| `.github/workflows/` | Three GitHub Actions workflows, all publishing: `build_images.yml` (ECR, on push to `main`), `publish_docker.yml` (Docker Hub, on a `v*` tag), `publish.yml` (PyPI, on a release). None of them runs the test suite. |
| `README.md` | Quickstart plus the full `NDIF_*` env table. |

## `src/ndif/cli/` — the `ndif` command

The entry point declared at `pyproject.toml:127` (`ndif = "ndif.cli:cli"`). A click
group that adds one command per module under `commands/`.

| Path | Role |
|---|---|
| `main.py` | The click group; registers every subcommand and layers `.env` before any command runs. |
| `config.py` | `DEFAULTS` (single-host NDIF_* fallbacks) and `build_env`, which assembles the environment handed to a spawned service. |
| `service.py` | The service registry: `SERVICES` (`service.py:66` — redis, minio, ray, api) and `OPTIONAL_SERVICES` (`service.py:75` — dashboard). This is where a new service gets registered. |
| `state.py` | PID/log files under `$NDIF_HOME` (default `~/.ndif`) for detached services. |
| `util.py` | Presentation helpers plus process signalling. |
| `commands/*.py` | One module per verb: `start`, `stop`, `restart`, `deploy`, `scale`, `evict`, `status`, `queue`, `kill`, `export`, `env`, `logs`, `info`, `doctor`, `version`. Click decoration only. |
| `lib/*.py` | The logic behind those verbs, importable without click. The dashboard backend calls this directly, so a `lib/` signature is a public-ish API. |

> **Gotcha:** `NDIF_RAY_HEAD_PORT` is `6385` (deliberately offset from Redis's
> 6379) both via `cli/config.py:29` and via `services/ray/start.sh:60`'s own
> `${NDIF_RAY_HEAD_PORT:-6385}` fallback, so the CLI, compose, and a hand-run
> `start.sh` all agree — and none collides with a local Redis.

## `src/ndif/common/` — shared by every service

| Path | Role |
|---|---|
| `providers/` | One module per external service (Redis, Ray, object store, Postgres, Loki, InfluxDB) behind the `Provider` base at `providers/base.py:26`. Classmethod singletons, env-driven, connect at import. See [adding-a-provider.md](./adding-a-provider.md). |
| `redis/` | Redis **key** constants only (the connection lives in `providers/redis.py`): the coalesced `/status` and `/env` caches and the dispatcher's operational event stream. |
| `schema/` | `BackendRequestModel` / `BackendResponseModel` (subclasses of nnsight's own request/response models, so the wire format is the client's) and the controller RPC contracts. |
| `metrics.py` | One class per InfluxDB measurement, deciding the tag/field split. |
| `telemetry.py` | `event(logger, msg, **fields)` — the structured-logging helper every service uses instead of raw `extra=`. |
| `logging_setup.py` | The console formatter and the record-field extraction the Loki JSON line reuses. |
| `types.py` | `MODEL_KEY`, `REPLICA_ID`, `NODE_ID` aliases. |

## `src/ndif/services/api/` — FastAPI ingress + the queue

```
api/
├── start.sh            exec gunicorn -c python:...gunicorn_conf ...app:app
├── gunicorn_conf.py    binds the port; forks workers; spawns the dispatcher
├── app.py              every HTTP/WS endpoint
├── auth.py             API-key verification; stamps trusted / priority / email
├── versioning.py       optional client nnsight/python version gate
└── queue/
    ├── config.py       NDIF_QUEUE_* / NDIF_AUTOSCALING_* knobs
    ├── dispatcher.py   pops the Redis queue, owns one Processor per model
    ├── processor.py    per-model queue + replica pool + autoscaling loop
    └── replica.py      one model actor; the worker task that dispatches to it
```

The dispatcher is not a separate container: `gunicorn_conf.py:61` starts it as a
**spawned** process from the gunicorn master. Spawn rather than fork, because the
Loki and Influx providers own background threads that would be dead in a forked
child.

## `src/ndif/services/ray/` — GPU side

```
ray/
├── start.sh            ray start (head or worker) then launches the controller
├── resources.py        prints this node's custom Ray resources as JSON
├── deployments/
│   ├── controller/
│   │   ├── controller.py       the detached Controller actor: build()/apply() diff
│   │   └── cluster/
│   │       ├── cluster.py      in-memory model of nodes + placement decisions
│   │       ├── node.py         one GPU node: HOT deployments and WARM cache
│   │       ├── deployment.py   one replica; the Ray actor create/delete/cache ops
│   │       └── evaluator.py    meta-device GPU footprint estimate, memoized
│   └── modeling/
│       ├── base.py     BaseModelDeployment (the run/execute template) + ModelActor
│       └── util.py     dtype resolution, device maps, CPU-relocating pickler
└── sandbox/
    ├── ARCHITECTURE.md the authoritative design doc for this subtree
    ├── model.py        SandboxModelDeployment + SandboxModelActor
    ├── host.py         spawn runners, Pool, Connection
    ├── runner.py       the runner process entry point
    ├── nns.py          nnsight patches that run *only inside the runner*
    └── protocol.py     framing, codec, and the message catalog
```

Whether a request's Python runs in the actor or in a runner is decided per request
by `request.trusted` (`sandbox/model.py:218`), not by which actor class is
deployed. Read `sandbox/ARCHITECTURE.md` before changing anything under
`sandbox/`; it is current and it is the design of record.

> **Gotcha:** a trusted request's block runs *in* the model actor process
> (`deployments/modeling/base.py`); only an untrusted one is diverted to a runner
> subprocess under `sandbox/`. Which path you get depends on `request.trusted`,
> so the actor you are reading may not be the one executing user code.

## `src/ndif/services/dashboard/` — admin UI

```
dashboard/
├── start.sh        writes /etc/cron.d/ndif-dashboard (if cron exists), execs uvicorn
├── backend/        FastAPI: auth, schedule CRUD, monitor reads, deploy/evict
│   └── ndif_client.py   thin wrapper over cli/lib — the dashboard's only door in
├── jobs/           cron entry points: monitor.py (uptime probes), reconcile.py
├── frontend/       Vue 3 + Vite SPA; frontend/dist/ is the built artifact
└── config.example.json
```

`frontend/dist/` is **committed and shipped** — the image never runs `npm`.
Rebuilding the UI is a host-side `cd frontend && npm ci && npm run build`.

> **Gotcha:** `dashboard/README.md` tells you to run `make build && make up` and
> `make dashboard-frontend`. There is no Makefile; the repo uses `just`.

## `docker/`

| Path | What it is |
|---|---|
| `Dockerfile` | One image for all services. `python:3.12-slim`, torch from a `--index-url` build arg (`TORCH_CUDA`, default `cu126`), `requirements.txt` in its own layer, then `pip install --no-deps ".[api,ray,metrics,postgres,dashboard]"` and the `ext` list read back out of `pyproject.toml`. `apt` adds `redis-server` and the minio binary is copied out of `quay.io/minio/minio`, so `NDIF_SERVICE=all` can run those in-container. `ndif version --write /etc/ndif/build.json` records what the build resolved to. `ENTRYPOINT ["ndif"]` + `CMD ["start", "--foreground"]`. |
| `docker-compose.yml` | The dev stack: redis, loki, influxdb, prometheus, grafana, postgres, minio, api, dashboard, ray. Every NDIF service uses the same `build:` block and differs only by `NDIF_SERVICE` + env. |
| `grafana/provisioning/` | Datasources (Loki, Influx, Prometheus, Postgres) and nine pre-built NDIF dashboards. |
| `prometheus/prometheus.yml` | Scrapes Ray's metrics-export port; Ray is the only service speaking Prometheus. |
| `postgres/init.sql` | The users/keys schema and the read-only `ndifapi` role. No seed data — auth is off unless you set `NDIF_POSTGRES_URL`. |

## `tests/` and `justfile`

`tests/` holds one live-server suite. `conftest.py` points
`nnsight.CONFIG.API.HOST` at `http://localhost:8001` and skips everything if
nothing answers `/ping`; `conftest_untrusted.py` is the same fixture set forced
onto the sandbox path. The test modules are `test_nnsight_remote.py` (the real
`remote=True` client path), `test_sandbox_conformance.py`, `test_fanout.py`,
`test_placement.py`, `test_node_registry.py`, `test_replica_wait.py`, and
`test_tensor_parallel_remote.py`. The three workflows in `.github/workflows/`
only publish — nothing runs this suite in CI. See [testing.md](./testing.md).

The `justfile` wraps one compose command: `just build`, `just up [service...]`,
`just down [-v]`, `just ta` (down → build → up), `just restart`, `just logs`,
`just ps`. Its one piece of logic is `NNSIGHT_PATH` — when nnsight is importable
on the host it adds `docker/docker-compose.nnsight.yml` as a second `-f`, which
bind-mounts that checkout over the image's copy.

## `pyproject.toml`

Three things to know when you change it.

**Extras gate optional subsystems**, and each one exists because the matching code
degrades cleanly without it: `api` (fastapi/gunicorn), `ray` (ray, transformers,
peft, zstandard), `metrics` (influxdb-client, python-logging-loki), `postgres`
(asyncpg), `dashboard` (pydantic-settings, bcrypt, ...), `dev` (ruff, pytest,
pytest-asyncio, httpx). Adding a provider means adding an extra — see
[adding-a-provider.md](./adding-a-provider.md).

**`package-data` ships the non-Python files** (`pyproject.toml:144`). Services
launch via `start.sh`, which setuptools would otherwise leave out of the wheel, so
`ndif start api` on a plain `pip install` would fail with "cannot run bash". Each
package declares its own:

```toml
[tool.setuptools.package-data]
"ndif.services.api" = ["start.sh"]
"ndif.services.ray" = ["start.sh"]
"ndif.services.dashboard" = [
    "start.sh",
    "config.example.json",
    "frontend/dist/*",
    "frontend/dist/**/*",
]
```

The dashboard's two glob lines are what put the **built Vue SPA** inside the
installed package; `backend/config.py` then defaults `frontend_dist` to
`<package>/frontend/dist`. A new service that ships a `start.sh` must add its own
entry here or it will work from a source checkout and break from a wheel.

**Version is `0.1.0`** (`pyproject.toml:7`). Nothing here is a stable public API.

## nnsight as a dependency

NDIF's server code imports `nnsight` heavily — the request/response schemas
subclass nnsight's, the sandbox patches nnsight's interleaver, and the model actor
constructs nnsight model wrappers. **nnsight is an ordinary pinned dependency**
(`requirements.txt`, `nnsight>=0.8.0rc1,<0.9` from PyPI), so the image is
self-contained — nothing in the docs should depend on a checkout being present.
For local client work, `just up` / `just ta` bind-mount your installed nnsight
over the image's copy via `docker/docker-compose.nnsight.yml` (resolved through
`NNSIGHT_PATH`); install it editable so the mount points at your source tree.
Consult nnsight's own docs at nnsight.net and the nnsight repository for anything
client-side.

> **Gotcha:** the specifier names the pre-release explicitly because 0.8 is one
> on PyPI. `pip` only considers pre-releases when the specifier does (or with
> `--pre`), so loosening it to `nnsight<0.9` resolves to the latest *stable*
> release instead — an 0.7 server against 0.8 clients.

## I want to change X, open Y

| I want to change... | Open |
|---|---|
| An HTTP endpoint, or what a request looks like at ingress | `src/ndif/services/api/app.py` |
| Who is allowed in, or how `trusted` is decided | `src/ndif/services/api/auth.py` |
| Queue behavior, batching, autoscaling triggers | `src/ndif/services/api/queue/{dispatcher,processor,replica,config}.py` |
| Where replicas get placed, or eviction policy | `src/ndif/services/ray/deployments/controller/cluster/{cluster,node}.py` |
| GPU footprint estimation | `.../controller/cluster/evaluator.py` |
| Ray actor lifecycle (create / delete / HOT↔WARM) | `.../controller/cluster/deployment.py` |
| How a model loads, or the per-request run template | `src/ndif/services/ray/deployments/modeling/base.py` |
| The trusted/untrusted execution fork | `src/ndif/services/ray/sandbox/model.py` |
| The sandbox wire protocol | `src/ndif/services/ray/sandbox/protocol.py` (+ `ARCHITECTURE.md`) |
| An `ndif` subcommand's flags | `src/ndif/cli/commands/<verb>.py` |
| What an `ndif` subcommand *does* | `src/ndif/cli/lib/<verb>.py` |
| Which services `ndif start` knows about | `src/ndif/cli/service.py` |
| A default port or URL for single-host runs | `src/ndif/cli/config.py` (`DEFAULTS`) |
| A connection to an external system | `src/ndif/common/providers/<name>.py` + a pyproject extra |
| A Redis key, channel, or stream name | `src/ndif/common/redis/{env,events,status}.py` |
| What a log line carries | `src/ndif/common/telemetry.py`, `src/ndif/common/logging_setup.py` |
| A metric's tags or fields | `src/ndif/common/metrics.py` |
| The dev stack's topology, ports, or env | `docker/docker-compose.yml` |
| What's installed in the image | `requirements.txt` (pins) + `pyproject.toml` (extras) |
| A Grafana dashboard | `docker/grafana/provisioning/dashboards/json/*.json` |
| The admin UI's behavior | `src/ndif/services/dashboard/backend/routers/*.py`, then rebuild `frontend/` |

## Related

- [architecture-overview.md](./architecture-overview.md) — the same system top-down rather than by directory.
- [adding-a-service.md](./adding-a-service.md) — what wiring a fourth service touches.
- [adding-a-provider.md](./adding-a-provider.md) — the `common/providers/` contract.
- [testing.md](./testing.md) and [contributing.md](./contributing.md) — how to run and land a change.
- [docs/operating/compose-stack.md](../operating/compose-stack.md) — the same `docker/` tree from an operator's angle.
