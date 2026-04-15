# CLAUDE.md — NDIF agent guide

This file orients agents working in the NDIF repo. It is standalone: you should not need to load anything else to start. When you need more depth on design decisions, read `NDIF.md` (the human-facing source of truth, ~1760 lines).

---

## What NDIF is

**NDIF** (National Deep Inference Fabric) is the server that executes [NNsight](https://github.com/ndif-team/nnsight) remote traces on shared GPU clusters. A client pickles intervention code + a model key, POSTs it to the API, NDIF routes it to a Ray actor holding that model, runs the user code inside a security sandbox, uploads results to MinIO, and streams status over Socket.IO.

Python **3.12+** (see `pyproject.toml`). Packaged with `uv`. The repo contains three first-party services (API, Ray, CLI) plus a standalone Monitor service. The README still mentions Python 3.10/conda — that is stale; trust `pyproject.toml`.

---

## Top-level layout

```
ndif/
├── CLAUDE.md                 ← this file (for agents)
├── NDIF.md                   ← long-form design doc (for humans)
├── README.md                 ← user-facing install / quick start
├── Makefile                  ← build + run shortcuts
├── pyproject.toml            ← uv-based Python project (3.12+)
├── .env.example              ← all config via env vars; defaults live here
│
├── cli/                      ← `ndif` Click CLI (native dev mode)
├── docker/                   ← Dockerfile + docker-compose.yml (primary dev mode)
├── scripts/                  ← one-shot smoke scripts (`test.py`, `redeploy.py`)
├── telemetry/                ← grafana dashboards + prometheus config
├── tests/                    ← pytest suite (most tests need --run-remote)
│
└── src/
    ├── common/               ← shared code between services
    │   ├── schema/           ← Backend{Request,Response,Result}Model, mixins
    │   ├── providers/        ← redis, objectstore (MinIO/S3), socketio, mailgun, ray
    │   ├── metrics/          ← InfluxDB metric classes
    │   ├── logging/          ← centralized logger setup
    │   ├── tracing/          ← OpenTelemetry / Jaeger
    │   └── types.py          ← MODEL_KEY, API_KEY, etc.
    │
    └── services/
        ├── api/              ← FastAPI + Gunicorn (Dispatcher lives here)
        │   └── src/
        │       ├── app.py            FastAPI app + endpoints
        │       ├── dependencies.py   request validation
        │       ├── db.py             PostgreSQL API-key store
        │       ├── config.py, gunicorn.conf.py
        │       └── queue/            Dispatcher + per-model Processor
        │
        ├── ray/              ← Ray cluster (Controller + ModelActors)
        │   └── src/ray/
        │       ├── start.py          controller startup
        │       ├── resources.py      resource detection
        │       ├── deployments/
        │       │   ├── controller/
        │       │   │   ├── controller.py
        │       │   │   ├── cluster/  cluster.py / node.py / deployment.py / evaluator.py
        │       │   │   └── gcal/     Google Calendar driven scheduling
        │       │   └── modeling/
        │       │       └── base.py   ModelActor (execution + sandbox invocation)
        │       └── nn/
        │           ├── backend.py    RemoteExecutionBackend (bridges NNsight)
        │           ├── ops.py        StdoutRedirect
        │           └── security/     sandbox — read this before touching it
        │               ├── protector.py
        │               ├── importer.py
        │               ├── guards.py
        │               ├── protected_objects.py
        │               ├── whitelist.py / whitelist.yaml
        │               └── README.md
        │
        ├── monitor/          ← standalone uptime monitor + Flask dashboard
        │                       deploys outside the docker stack via run.sh + cron
        │
        └── base/             ← shared base image requirements
```

---

## Architecture at a glance

Three services, four infra dependencies:

| Component | Role |
|---|---|
| **API** (FastAPI + Gunicorn) | HTTP entry, validation, hosts Dispatcher + Processors |
| **Ray** (head + workers) | Controller actor + ModelActor workers |
| **CLI** (`ndif`) | Native service lifecycle + ops commands |
| Redis | Queue, pub/sub, Redis streams, Socket.IO backend |
| MinIO | S3-compatible object store for results/responses |
| PostgreSQL | API keys + tier assignments |
| Prometheus/InfluxDB/Grafana/Loki/Jaeger | Metrics, logs, traces |

**Request path:** client → `POST /request` → validate (API key, nnsight version, python version, hotswap tier) → pickle to Redis `queue` list → Dispatcher `brpop` → per-`model_key` Processor → Controller deploys the model (may evict others) → ModelActor `pre()` deserializes under a deserialization whitelist → `execute()` runs `RemoteExecutionBackend` under the `Protector` sandbox in a worker thread → `post()` uploads result to MinIO, emits `COMPLETED` over Socket.IO.

**Status lifecycle:** `RECEIVED → QUEUED → DISPATCHED → RUNNING → (LOG/STREAM…) → COMPLETED | ERROR`.

**Processor state machine:** `UNINITIALIZED → PROVISIONING → DEPLOYING → READY ↔ BUSY → CANCELLED`.

**Deployment levels:** `HOT` (on GPU) / `WARM` (CPU cache) / `COLD` (disk). The Controller's `build()`/`apply()` cycle produces a `DeploymentDelta` and executes it in order: **delete → cache → from_cache → create** (ordering is load-bearing — later steps need GPU freed by earlier ones).

**Deploy vs hotswap:** Dedicated deployments (startup, CLI, Google Calendar schedule) are never evicted. Hotswapping is on-demand and requires the `hotswapping` tier on the API key; it can evict non-dedicated models after `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS`.

**Why Redis for the queue (not Ray):** the Dispatcher lives inside the API process as a Ray *client*; Redis is the lingua franca between the FastAPI endpoint, the Dispatcher, and the Socket.IO layer (which needs a cross-process broker anyway).

See `NDIF.md` §2–§6 for the full data flow and §5.6 for the build/apply ordering rationale.

---

## Security sandbox (the highest-stakes area)

User-submitted intervention code runs inside a layered sandbox. **Never loosen guards without understanding what each one blocks.** The layers:

1. **`Protector`** (`nn/security/protector.py`) — context manager that patches `__import__`, `compile`, `exec`, and `StreamTracer.execute`. Applied around user code only; NNsight internals run unpatched.
2. **`Importer`** — whitelisted modules come back wrapped in immutable `ProtectedModule`s; non-whitelisted imports return a lazy `UnauthorizedModule` that only errors on *use* (so libraries can import optional deps they don't touch).
3. **`guarded_getattr`** + dunder allow/block lists — blocks sandbox-escape vectors (`__class__`, `__globals__`, `__code__`, `__reduce__`, `__subclasses__`, `__dict__`, …) and lets safe dunders (`__len__`, `__iter__`, arithmetic, comparison, context managers) through.
4. **`ProtectedObject`** — wraps the tokenizer/model. `.to()` is blocked; tensor/list/dict attributes are deep-copied on access so user code can't mutate the shared model.
5. **`SafeBuiltins`** + full builtin patching during execution — `open`, raw `exec`/`compile`, `eval` are gone.
6. **Two whitelists** in `whitelist.yaml`: the **execution** set (`torch`, `numpy`, `math`, `einops`, …) and a **broader deserialization-only** set (`pickle`, `cloudpickle`, `transformers`, `nnsight.schema.request`, …) used just during `pre()`.

**Note:** compile uses stock `compile()`, not RestrictedPython's AST transform, because RestrictedPython collides with NNsight's internal variable names. Enforcement is runtime via the guards. This is intentional — don't "fix" it by reintroducing AST restriction.

Before changing anything under `nn/security/`: read `src/services/ray/src/ray/nn/security/README.md` and run `pytest tests/test_security_guards.py --run-remote` against a live stack.

---

## Dev workflow — Docker is the default

Primary loop is Docker Compose. The compose file bind-mounts your local `nnsight` install into the Ray container so you can develop `nnsight` and NDIF together. The path is resolved automatically by the Makefile:

```makefile
# Makefile
NNSIGHT_PATH ?= $(shell python -c "import nnsight, os; print(os.path.dirname(nnsight.__file__))" 2>/dev/null)
```

```yaml
# docker/docker-compose.yml (ray service)
volumes:
  - ${NNSIGHT_PATH}:/usr/local/lib/python3.12/site-packages/nnsight/
```

`make up` depends on a `check-nnsight` target that fails fast with a clear error if `nnsight` isn't importable from whichever `python` is on PATH. Two ways to satisfy it:

- Install nnsight into your active env: `pip install nnsight`, or `pip install -e /path/to/nnsight` for a dev checkout.
- Or override explicitly: `export NNSIGHT_PATH=/absolute/path/to/nnsight` before running `make`.

Do not remove the mount — the expectation is that NDIF is developed alongside a local nnsight.

### Everyday commands

```bash
make build          # build api:latest + ray:latest from docker/Dockerfile (NAME build-arg)
make up             # bring up full stack (redis, minio, postgres, ray, api, prom, influx, grafana, loki, jaeger)
make down           # tear down
make ta             # down + build + up  ← use this after code edits
```

First-time setup: `make build && make up`. After editing source: `make ta`. If you only touched mounted code (e.g. nnsight), `make down && make up` may suffice.

### Verifying the stack is alive

```bash
docker logs dev-api-1        # expect "Application startup complete."
docker logs dev-ray-1
python scripts/test.py       # end-to-end smoke: runs a GPT-2 trace against http://localhost:5001
```

### Config

Everything is env-var driven. `.env.example` has defaults and is loaded by both the Makefile and compose. Override via a `.env` file next to it. Key ports (defaults):

| Port | Service |
|---|---|
| 5001 | API (NDIF_API_PORT) |
| 6379 | Redis / broker |
| 27018 | MinIO S3 |
| 46805 | MinIO console |
| 10001 | Ray client |
| 8265 | Ray dashboard |
| 5432 | Postgres |

`NDIF_DEV_MODE=true` (the default in `.env.example`) bypasses API-key validation — convenient locally, required for most of the smoke tests to work without a seeded Postgres.

### Native mode (`ndif` CLI)

The Click CLI in `cli/` can run the stack natively (`ndif start`, `ndif stop`, `ndif deploy <model_key>`, `ndif status`, `ndif logs <service>`, `ndif queue`, `ndif kill <id>`, `ndif info`, `ndif env`, `ndif export`). Sessions live in `~/.ndif/`. **Prefer Docker for development** — native mode is useful for one-off debugging and for running Ray worker nodes (`ndif start --worker` on a second machine).

---

## Testing

```bash
# Most tests in tests/ require a running NDIF stack. Start compose first.
cd tests
pytest --run-remote                         # run the full remote suite against localhost:5001
pytest tests/test_security_guards.py --run-remote   # after sandbox edits
pytest tests/test_hotswapping.py  --run-remote      # after scheduler/controller edits
pytest tests/test_user_code.py    --run-remote      # after changes that affect user-code deserialization
```

`tests/conftest.py` skips remote-dependent test classes unless `--run-remote` is passed, and configures nnsight to hit `--ndif-host` (default `http://localhost:5001`). Set `NDIF_HOST` in the env to override.

**Minimum bar for a change:** bring the stack up (`make ta`) and run the `--run-remote` pytest suite for the area you touched. `scripts/test.py` is a fine smoke check but not a substitute.

---

## Services beyond API/Ray/CLI

- **`src/services/monitor/`** — standalone uptime monitor. Not part of the docker stack; deployed separately via `run.sh` + cron into `~/ndif_monitor/`. Hits `/connected` every 10 min, runs nnsight traces on HOT models every 2 hours, posts Discord notifications on state change, and serves a Flask dashboard on port 8080. Has its own `README.md`.
- **`src/services/ray/.../controller/gcal/`** — Google Calendar scheduler. Reads a calendar to drive dedicated deployments at specific times. Config lives in compose env (`SCHEDULING_GOOGLE_CALENDAR_ID`, `SCHEDULING_GOOGLE_CREDS_PATH`).
- **`docker/postgres/`** — Postgres init SQL. Provides the dev-mode auth/API-key store wired into compose.

---

## Things to know before making changes

- **Whitelist edits rebuild Ray.** `whitelist.yaml` is packaged into the Ray image; edits require `make ta` (or at minimum a ray-image rebuild + restart) to take effect.
- **`ModelActor` uses `num_gpus=0` on purpose.** GPU assignment is done via `CUDA_VISIBLE_DEVICES` so the Controller — not Ray's scheduler — owns GPU placement. Don't "fix" this by handing GPU scheduling back to Ray.
- **Ray client deadlock patch.** The Dispatcher monkey-patches `DataClient._async_send` to work around a Ray lock contention bug. It's intentional; see `queue/util.py`.
- **CUDA device-side asserts are terminal.** A device-side assertion corrupts the CUDA context, so the ModelActor self-kills with `no_restart=False` and lets Ray respawn it. Expect this path if you see that error string.
- **Thread kills are `ctypes`-based.** Timeouts / cancellations fire a `SystemExit` into the execution thread via `kill_thread()`. It's a last resort — not clean, but it prevents runaway user code from blocking the actor.
- **`build()`/`apply()` ordering is load-bearing.** Delete → cache → from_cache → create. Reordering will starve later steps of GPUs.
- **Deserialization has a separate, broader whitelist.** It is only active inside `ModelActor.pre()`. Execution uses the narrower set. Don't collapse them.
- **Dev-mode Postgres.** `NDIF_DEV_MODE=true` skips API-key lookups entirely; nothing touches Postgres in that path. Tests rely on this.
- **Two compose mount assumptions** that may not hold on a given machine: the nnsight source mount (above) and the HuggingFace cache mount (`~/.cache/huggingface`). Update both if your paths differ.

---

## Pointers for common tasks

| Task | Start here |
|---|---|
| Add an API endpoint | `src/services/api/src/app.py` + `dependencies.py` |
| Change request validation | `src/services/api/src/dependencies.py`, `src/common/schema/request.py` |
| Change routing / queue behavior | `src/services/api/src/queue/dispatcher.py`, `queue/processor.py` |
| Change cluster scheduling / eviction | `src/services/ray/src/ray/deployments/controller/cluster/{cluster,node,evaluator}.py` |
| Change model lifecycle (HOT/WARM/COLD) | `src/services/ray/src/ray/deployments/controller/cluster/deployment.py`, `controller.py` |
| Change execution / cleanup | `src/services/ray/src/ray/deployments/modeling/base.py`, `src/services/ray/src/ray/nn/backend.py` |
| Change the sandbox | `src/services/ray/src/ray/nn/security/` (read its README first) |
| Change result/response storage | `src/common/schema/{result,response}.py`, `src/common/schema/mixins.py`, `src/common/providers/objectstore.py` |
| Change env/config | `.env.example` + the relevant service `config.py` |
| Change CLI commands | `cli/commands/` |

---

## When to read `NDIF.md`

`NDIF.md` is the long-form design doc. Read the relevant section when:

- You are changing the Dispatcher, Processor, or request lifecycle → §4
- You are changing the Controller, Cluster, Node, or Evaluator → §5
- You are changing ModelActor execution, streaming, or cleanup → §6
- You are changing anything in `nn/security/` → §7
- You are changing schemas or the object-storage mixin → §8

For everything else, this file + the code itself should be enough.
