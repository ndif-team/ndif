---
title: Configuration
one_liner: How NDIF is configured — environment variables only, no config file, layered per process, with a working single-host default for everything.
tags: [operating, cli, dev, telemetry, auth]
related: [docs/reference/env-vars.md, docs/operating/compose-stack.md, docs/operating/quickstart.md, docs/operating/production.md, docs/operating/cli.md, docs/developing/providers.md, docs/reference/ports.md, docs/runbooks/enable-auth.md, docs/gotchas/networking-and-compose.md]
sources: [src/ndif/cli/config.py, src/ndif/common/providers/base.py, docker/docker-compose.yml, docker/Dockerfile, pyproject.toml, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/dashboard/backend/config.py]
---

# Configuration

## What this covers

The *model* of configuration — where values come from, what wins, what crosses a
process boundary, and which parts of it you must change to run somewhere other
than one laptop. The exhaustive variable-by-variable table lives in
`docs/reference/env-vars.md`; this page deliberately doesn't repeat it.

Two facts frame the whole design:

1. **There is no config file.** No YAML, no TOML, no `settings.py`. Every knob is
   an `NDIF_*` environment variable, and nearly all of them are read once at
   process start. Changing a value means restarting the process that reads it.
2. **Every knob has a working single-host default.** Each service and each
   provider carries its own defaults, tuned so that a bare `ndif start` on one
   machine — Redis on `localhost:6379`, MinIO on `localhost:9000`, the API on
   `localhost:8001` — comes up with nothing set. Config is therefore additive:
   you set variables to *move away* from single-host, not to get started.

The cost of (1) is that there is no single place to look at "the config". The
benefit is that every process is self-describing: whatever is in its environment
is its entire configuration, and nothing is inherited from a service you forgot
about.

## Where defaults live

Four shapes, all equivalent in effect:

- A **provider `CONFIG` spec** — `{"url": ("NDIF_REDIS_URL", "redis://localhost:6379", str)}`.
  `Provider.from_env()` walks it and sets class attributes, casting the env
  string (`Provider.from_env`, `src/ndif/common/providers/base.py:39-43`). This covers Redis, the
  object store, Postgres, Loki and Influx.
- A bare **`os.environ.get(name, default)`** — the queue config, the controller's
  `ControllerDeploymentArgs`, gunicorn's settings.
- A **`${VAR:-default}` fallback in a `start.sh`** — the Ray node's ports and
  timings.
- A **pydantic `Field(default=...)`** on the dashboard's `Settings`, matched to a
  variable by `env_prefix = "NDIF_DASHBOARD_"` (`dashboard/backend/config.py:38`).

## Precedence

Later layers win.

1. **Code defaults**, as above.
2. **CLI defaults** — the `DEFAULTS` dict in `src/ndif/cli/config.py:22-31`,
   merged *underneath* the real environment by `build_env` (`config.py:60`:
   `{**DEFAULTS, **os.environ}`). They exist to fix the handful of code defaults
   that are wrong for a single host — most importantly `NDIF_RAY_HEAD_PORT`,
   which the CLI sets to **6385** because Ray's own GCS default is 6379, the same
   port as Redis.
3. **`.env` files** — `config.load_env_files` runs before any CLI command
   (`config.py:42-46`). A CWD-relative `./.env` is loaded *without* `override`,
   so it only fills gaps the shell left; an explicit `ndif --env-file path`
   is loaded with `override=True` and beats the shell.
4. **The shell environment** of the process.
5. **Per-invocation CLI overrides** — `ndif start -e KEY=VALUE` (repeatable) and
   the four typed shortcuts `--redis-url`, `--ray-address`, `--ray-head-address`,
   `--api-port` (`config.py:34-39`). These are applied last and win outright.

Under Docker, the container entrypoint is `ndif start --foreground`
(`docker/Dockerfile:49`), so **the CLI layer applies inside compose too**. Both
the CLI `DEFAULTS` and `ray/start.sh:60` fall back to the same
`NDIF_RAY_HEAD_PORT` of 6385, so the head port is unambiguous however Ray is
launched. The compose `environment:` block is layer 4 (the process
environment) as far as the CLI is concerned, and beats everything except an
explicit `-e`.

> **Gotcha:** the auto-discovered `.env` is relative to the **current working
> directory**, which inside every NDIF container is `/app` (`Dockerfile:20`) —
> a directory with no `.env` in it. Configuring a container through `.env`
> requires bind-mounting the file to `/app/.env`. The compose file also uses no
> `${VAR}` interpolation and no `env_file:` key, so a `docker/.env` would have
> nothing to substitute into. In compose, `environment:` blocks are the layer
> that matters.

## One boundary config crosses by itself

A Ray worker inherits only its node's ambient environment, not the controller's.
So when the controller creates a model actor, it explicitly exports its own
Redis, object-store, Loki and Influx settings into the actor's Ray
`runtime_env` (`cluster/deployment.py:16-39`, applied at `deployment.py:174-196`),
plus `NDIF_SERVICE=model` so the actor's telemetry attributes correctly.

The practical consequence: **you configure model actors by configuring the `ray`
service.** There is no separate actor environment to set, and a value you set on
the head propagates to actors placed on any node.

## What must differ from the dev defaults

Everything below has a default that only makes sense on one machine. This is the
short list — see `docs/reference/env-vars.md` for the full set and the exact
reader of each.

| Variable | Dev default | Why it must change |
|---|---|---|
| `NDIF_REDIS_URL` | `redis://localhost:6379` | Must name the real Redis host from *every* service. Inside the `ray` container, `localhost:6379` is Ray's own GCS. |
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | The API/dashboard/CLI client address of the head node. |
| `NDIF_RAY_HEAD_ADDRESS` | *(empty)* | Empty ⇒ this node starts a head. Every worker node must set it to the head's `HOST:6385`. |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | The endpoint the *server* uploads through. Empty means real AWS S3 for `NDIF_OBJECT_STORE_REGION`. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | *(empty → falls back to the above)* | The endpoint presigned URLs are *signed* with. Must be reachable by clients. |
| `NDIF_OBJECT_STORE_ACCESS_KEY` / `_SECRET_KEY` | `minioadmin` / `minioadmin` | Real credentials. |
| `NDIF_POSTGRES_URL` | *(empty)* | Empty ⇒ no auth ⇒ every request trusted. See below. |
| `NDIF_API_URL` | `http://localhost:8001` | The address other components use to reach the API (compose sets `http://api:8001`). |
| `NDIF_DASHBOARD_DEV_MODE` | `false` in code, **`true` in compose** | `true` disables the dashboard login entirely. |
| `NDIF_DASHBOARD_SESSION_SECRET` | `change-me-please-this-is-not-secure` | Anyone with the default can forge a session cookie. |
| `NDIF_DASHBOARD_PASSWORD_HASH` | *(empty)* | Required once dev mode is off. |
| `NDIF_ENVIRONMENT` | `dev` | The prod/staging/dev label on every log line and metric point. |
| `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | in-process `ModelActor` in code, `SandboxModelActor` in compose | Decides whether untrusted user code gets a separate runner process at all. |

That last row is worth dwelling on: the code default and the compose value
**differ**. `just up` runs `ndif.services.ray.sandbox.model.SandboxModelActor`
(`docker-compose.yml:228`); a plain `pip install` plus `ndif start ray` runs the
base in-process actor. If you are reproducing a compose behavior outside compose,
set the variable.

## The fail-open switches

Several subsystems are off by default and turn on when you give them a URL. An
empty value is never an error; it silently disables the feature.

| Empty value | Effect |
|---|---|
| `NDIF_LOKI_URL` | No log shipping. The console handler is always configured, so logs still reach stdout and `just logs` (`providers/loki.py:159-180`). The `logging_loki` package is never even imported. |
| `NDIF_INFLUX_*` | Metrics are dropped. `NDIF_INFLUX_ENABLED=false` short-circuits `connect` outright (`providers/influx.py:112`); with it true but no reachable server or valid token, points are buffered and discarded by the background writer. Either way nothing blocks and nothing raises. |
| `NDIF_POSTGRES_URL` | **Not a telemetry switch.** See below. |

Postgres is deliberately shaped the opposite way from the telemetry providers.
If the URL is set but `asyncpg` isn't installed, `connect` raises loudly rather
than quietly running without auth — "auth silently off" would be a security hole,
whereas "no metrics" is harmless (`providers/postgres.py:12-17`).

**What an empty `NDIF_POSTGRES_URL` really means.** It is not just "no API keys
required". `verify_api_key` returns `None` when Postgres is unconfigured, and
`validate_request` then defaults `request.trusted` to `True` when the request
doesn't set it (`src/ndif/services/api/auth.py:180`). `trusted` is the fork that decides how
user code executes: a trusted request's traced block runs **in-process inside the
model actor, next to the weights**, with no runner subprocess; and the same flag
becomes `trust_remote_code=` when the model loads
(`cluster/cluster.py:169`). So an unauthenticated NDIF runs every caller's
arbitrary Python in-process and loads models with remote code execution enabled.
That is the trade the zero-config default makes. `docs/concepts/auth-and-limits.md`
and `docs/runbooks/enable-auth.md` cover flipping it.

## Pip extras

The package's core dependency set is deliberately small — nnsight, pydantic,
redis, click, python-dotenv, pyyaml, boto3 (`pyproject.toml:21-34`). Everything
else is an extra, so a machine that only runs one role installs only that role's
dependencies.

| Extra | Pulls in | Unlocks |
|---|---|---|
| `api` (`pyproject.toml:37`) | fastapi, uvicorn, gunicorn, python-multipart | Running `ndif start api` — the HTTP surface and the queue dispatcher. |
| `ray` (`:43`) | ray[default], transformers, accelerate, numpy, zstandard, peft | Running a Ray node and the model actors: loading checkpoints, decompressing request payloads (nnsight compresses by default), applying per-request PEFT adapters. |
| `dashboard` (`:81`) | fastapi, uvicorn, pydantic-settings, itsdangerous, bcrypt, requests | `ndif start dashboard`. The reconcile cron additionally needs the `ray` extra; the monitor cron's probes of PEFT/VLM checkpoints need `peft` / `torchvision`. |
| `metrics` (`:62`) | influxdb-client, python-logging-loki | Telemetry shipping. Both providers fail open without it — services run console-only and metrics-free. |
| `postgres` (`:71`) | asyncpg | API-key auth. Without it (or without the URL), the API runs unauthenticated. |
| `dev` (`:90`) | ruff, httpx, pytest, pytest-asyncio | The live-server test suite under `tests/`. |

The docker image takes `[api,ray,metrics,postgres,dashboard]` in a single install
so one image can play any role (`docker/Dockerfile:45`). It uses `--no-deps`
because `requirements.txt` already pins every transitive dependency — the extras
there only declare intent, so a source change reinstalls the package alone rather
than re-resolving multiple GB of wheels.

## Checking what a process actually sees

```bash
ndif info                # CLI config, tracked PIDs, endpoint reachability
ndif doctor              # probes redis / minio / api / ray from the same vars
ndif env                 # the cluster's python version + installed packages
```

`ndif info` prints the resolved value of `NDIF_REDIS_URL`,
`NDIF_OBJECT_STORE_URL`, `NDIF_API_URL` and `NDIF_RAY_ADDRESS` alongside a
reachability check for each (`cli/commands/info.py:13-19`) — the fastest way to
find out that a variable you thought you set didn't reach the process. Inside
compose, run it in the container whose config you're questioning:

```bash
docker compose -f docker/docker-compose.yml exec ray ndif info
```

## Gotchas

- **Values are read at import, not per request.** Changing a variable requires
  restarting the process — and for anything baked into the image, a `just ta`.
- **A missing variable never falls back to another service's value.** It falls
  back to that process's hardcoded default, which is almost always `localhost`.
  Most cross-service breakage in this stack is a `localhost` default that nobody
  overrode.
- **`NDIF_SERVICE` does double duty**: it selects which service `ndif start`
  launches *and* becomes the `service` label on every log line and metric point.
- **The dashboard reads `NDIF_DASHBOARD_API_URL` first, then `NDIF_API_URL`**
  (`dashboard/backend/config.py:49-52`), so setting only the shared variable is
  enough.

## Related

- `docs/reference/env-vars.md` — every variable, its default, and the line that reads it.
- `docs/operating/compose-stack.md` — the `environment:` blocks in context.
- `docs/operating/production.md` — the minimum set to change before going live.
- `docs/developing/providers.md` — how `CONFIG` specs and fail-open behavior are implemented.
- `docs/runbooks/enable-auth.md` — turning `NDIF_POSTGRES_URL` on properly.
