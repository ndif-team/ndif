---
title: Configuration
one_liner: How NDIF is configured — environment variables only, no config file, layered per process with a fixed precedence, and the handful of defaults that must change off a single host.
tags: [operating, cli, dev, telemetry, auth]
related: [docs/reference/env-vars.md, docs/operating/compose-stack.md, docs/operating/quickstart.md, docs/operating/production.md, docs/operating/cli.md, docs/developing/providers.md, docs/reference/ports.md, docs/runbooks/enable-auth.md]
sources: [src/ndif/cli/config.py, src/ndif/common/providers/base.py, docker/docker-compose.yml, docker/Dockerfile, pyproject.toml, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/api/versioning.py, src/ndif/services/dashboard/backend/config.py, docker/docker-compose.egress.yml, docker/egress/allowlist.py, src/ndif/services/ray/sandbox/egress.py]
---

# Configuration

## What this covers

The *model* of configuration — where values come from, what wins, what crosses a
process boundary, and what must change to run anywhere but one machine. Every
variable with its default and reader is in
[Environment Variables](../reference/env-vars.md).

- **There is no config file.** Every knob is an `NDIF_*` environment variable,
  read once at process start. Changing a value means restarting the process that
  reads it — and for anything baked into the image, a `just ta`.
- **Every knob has a working single-host default**, so a bare `ndif start` —
  Redis on `localhost:6379`, RustFS on `localhost:9000`, the API on
  `localhost:8001` — comes up with nothing set. You set variables to *move away*
  from single-host.
- **A missing variable never falls back to another service's value.** It falls
  back to that process's own default, which is almost always `localhost`. Most
  cross-service breakage is a `localhost` default nobody overrode.

Defaults live in four places: a provider's `CONFIG` spec, read by
`Provider.from_env` (`src/ndif/common/providers/base.py`); a bare
`os.environ.get` (the queue config, `ControllerDeploymentArgs`, gunicorn); a
`${VAR:-default}` in a `start.sh`; and the dashboard's pydantic `Settings`,
which reads the `NDIF_DASHBOARD_` prefix.

## Precedence

Lowest to highest; later layers win.

1. **Code defaults**, as above.
2. **CLI defaults** — `DEFAULTS` in `src/ndif/cli/config.py`, merged *underneath*
   the real environment by `build_env`. They exist for the few code defaults
   that are wrong on a single host; most importantly `NDIF_RAY_HEAD_PORT=6385`,
   because Ray's own GCS default is 6379, the same port as Redis.
3. **`./.env`** in the current directory — loaded by `load_env_files` without
   `override`, so it only fills gaps the shell left.
4. **The shell environment** of the process (under compose, the service's
   `environment:` block).
5. **`ndif --env-file PATH`** — loaded with `override=True`, so it beats the shell.
6. **Per-invocation overrides** — `ndif start -e KEY=VALUE` (repeatable) and the
   typed shortcuts `--redis-url`, `--ray-address`, `--ray-head-address`,
   `--api-port` (`ENV_OPTIONS`). Applied last.

Under Docker the entrypoint is `ndif start --foreground`, so the CLI layers
apply inside every container too. Two consequences:

- The auto-discovered `.env` is relative to the working directory, which in
  every NDIF container is `/app` — a directory with no `.env`. Configuring a
  container through `.env` means bind-mounting the file to `/app/.env`.
- The compose file uses `${VAR}` interpolation only for `HF_TOKEN`, `HOME` and
  `NDIF_VERSION`, and has no `env_file:` key. Everything else is set in each
  service's `environment:` block.

## One boundary config crosses by itself

A Ray worker inherits only its node's ambient environment, not the controller's.
So when the controller creates a model actor it exports its own Redis,
object-store, Loki and Influx settings into the actor's Ray `runtime_env`
(`src/ndif/services/ray/deployments/controller/cluster/deployment.py`
`_provider_runtime_env`, applied in `Deployment.create`), plus
`NDIF_SERVICE=model` so the actor's telemetry attributes correctly. The
dispatcher actor runs on the head and reads the head node's environment; its
launcher sets only `NDIF_SERVICE=dispatcher`
(`src/ndif/services/ray/queue/dispatcher.py` `app`).

**You configure model actors and the dispatcher by configuring the `ray`
service** on the head.
There is no separate actor environment, and a value set on the head reaches
actors placed on any node.

## What must differ from the dev defaults

| Variable | Dev default | Why it must change |
|---|---|---|
| `NDIF_REDIS_URL` | `redis://localhost:6379` | Must name the real Redis host from *every* service. Inside the `ray` container nothing listens on `localhost:6379`. |
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | The dashboard/CLI client address of the head node. |
| `NDIF_RAY_HEAD_ADDRESS` | *(empty)* | Empty ⇒ this node starts a head. Every worker node sets it to the head's `HOST:6385`. |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | The endpoint the *server* uploads through. Empty means real AWS S3 for `NDIF_OBJECT_STORE_REGION`. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | *(empty → the above)* | The endpoint presigned URLs are *signed* with. Must be reachable by clients. |
| `NDIF_OBJECT_STORE_ACCESS_KEY` / `_SECRET_KEY` | `rustfsadmin` / `rustfsadmin` | Real credentials, or both empty to use boto3's own chain (on AWS, the instance/task role). |
| `NDIF_POSTGRES_URL` | *(empty)* | Empty ⇒ no auth: client-supplied `tags` are honored and unspecified ones default to `{"trusted"}`. See below. |
| `NDIF_MODEL_IMPORT_PATH` | in-process `ModelActor` in code; compose sets `NDIF_DEFAULT_MODEL_ACTOR_CLASS` to `SandboxModelActor` | Decides whether an untrusted request gets a separate runner process at all. See below. |
| `NDIF_API_URL` | `http://localhost:8001` | Where the CLI (`env`, `doctor`, `info`) and the dashboard's report cron reach the API. Compose sets `http://api:8001` on `dashboard`. |
| `NDIF_DASHBOARD_DEV_MODE` | `false` in code, **`true` in compose** | `true` disables the dashboard login entirely. |
| `NDIF_DASHBOARD_SESSION_SECRET` | `change-me-please-this-is-not-secure` | Anyone with the default can forge a session cookie. |
| `NDIF_DASHBOARD_PASSWORD_HASH` | *(empty)* | Required once dev mode is off. |
| `NDIF_ENVIRONMENT` | `dev` | The prod/staging/dev label on every log line and metric point. |

**The actor class.** The controller reads `NDIF_MODEL_IMPORT_PATH`, falls back
to `NDIF_DEFAULT_MODEL_ACTOR_CLASS`, and falls back to the in-process
`ndif.services.ray.deployments.modeling.base.ModelActor`
(`src/ndif/services/ray/deployments/controller/controller.py`
`ControllerDeploymentArgs`). Compose sets the second to
`ndif.services.ray.sandbox.model.SandboxModelActor`; the image and a from-source
install set neither, so they run untrusted code in-process. To reproduce compose
behavior outside compose, set it. See
[Going to Production](production.md#the-sandboxed-actor-class).

**An empty `NDIF_POSTGRES_URL`.** `verify_api_key` returns `None`, and
`validate_request` keeps `tags` the client sent and sets them to `{"trusted"}` when
the client sent none (`src/ndif/services/api/auth.py` `validate_request`). The
nnsight client sends none, so its requests run trusted: the traced block runs
in-process in the model actor, and it may provision a model whose key carries
`trust_remote_code`. [Auth and Limits](../concepts/auth-and-limits.md) and
[Enable auth](../runbooks/enable-auth.md) cover turning it on.

## Confining untrusted code to an egress proxy

An untrusted block has the host's network unless you set `NDIF_SANDBOX_EGRESS`.
With it set, runners start in a network namespace that holds only loopback and
reach one thing: an egress proxy that you run. The proxy is mitmproxy 12 with
the policy in `docker/egress/allowlist.py`, which reads two comma-separated
lists of host names from its own environment. A name covers its subdomains.

| Variable, on the proxy | A host on the list |
|---|---|
| `EGRESS_READ_ONLY` | is decrypted, and only `GET` and `HEAD` are forwarded |
| `EGRESS_ALLOW` | is passed through untouched |

Every other host is refused. With `EGRESS_READ_ONLY=huggingface.co,hf.co` and
`EGRESS_ALLOW` empty, a block can download a file published on the Hub, and the
proxy forwards nothing else.

On the compose stack, add the override file. It starts the proxy with those two
values and sets `NDIF_SANDBOX_EGRESS` on `ray`. To change the lists, edit them
there.

```bash
docker compose -f docker/docker-compose.yml -f docker/docker-compose.egress.yml up -d
```

Two things differ in a container:

- The override takes docker's default seccomp profile off the whole `ray`
  container, because that profile refuses the namespace.
- The image runs as root, and a confined root loses root's pass on files that
  other users own. Its runners read the mounted Hugging Face cache only if root
  owns it or its mode lets anyone. Behind a cache directory that is `0700` and
  yours, a runner has no tokenizer or module map, and a block cannot download
  into a directory that only your user may write.

Without docker, run the proxy where the model actors can reach it. One on each
GPU node, listening on loopback, needs no network rule:

```bash
EGRESS_READ_ONLY=huggingface.co,hf.co mitmdump \
    --listen-host 127.0.0.1 --listen-port 3128 \
    --set flow_detail=1 --set stream_large_bodies=1m -s docker/egress/allowlist.py

export NDIF_SANDBOX_EGRESS=http://127.0.0.1:3128   # before `ray start` on that node
```

- The host has to allow unprivileged user namespaces: `unshare -Urn true`
  succeeds as the user Ray runs as. Where it does not, a runner exits before it
  runs anything and untrusted requests fail
  ([Troubleshooting](troubleshooting.md#symptom--diagnosis--fix)).
- Only an untrusted request on a sandbox actor class is confined
  (`NDIF_MODEL_IMPORT_PATH`, above). A trusted request runs in the model actor,
  with the host's network. A confined actor's log has
  `sandbox: runners are confined to the egress proxy at <host>:<port>`.
- The proxy logs each request it forwards, and each one it refuses with the
  reason (`refused ...`).
- Keep the proxy's data directory (`~/.mitmproxy`) across restarts. It holds the
  certificate authority that runners are told to trust, and an actor fetches it
  once. After a proxy that lost it, a block's HTTPS requests fail until the
  model is redeployed.
- A runner starts whether or not the proxy is up. While it is down, a block's
  requests fail.
- It closes a block's network route and is not a hardened boundary: a block
  still runs as the actor's user, with its files and its view of the host's
  other processes
  ([Sandbox Execution](../concepts/sandbox-execution.md#what-the-separation-is--and-is-not)).

## The fail-open switches

Telemetry is off until you give it a URL, and an empty value is never an error:
an empty `NDIF_LOKI_URL` or `NDIF_INFLUX_URL` turns that sink off and nothing
blocks or raises ([Telemetry Internals](../developing/telemetry-internals.md#fail-open-behavior)).

Postgres is the opposite: with the URL set but `asyncpg` not installed,
`PostgresProvider.connect` raises rather than running without auth — silent
"auth off" would be a security hole, where silent "no metrics" is harmless.

## The client version gate

The nnsight client stamps `nnsight-version` and `python-version` headers on every
`POST /request`, and `src/ndif/services/api/versioning.py`
`validate_client_versions` checks them once the request has been authenticated.

| Variable | Example | Unset or empty |
|---|---|---|
| `NDIF_MIN_NNSIGHT_VERSION` | `0.8.0` | no nnsight gating |
| `NDIF_MIN_PYTHON_VERSION` | `3.10` | no python gating |

Both default to unset, so **an out-of-the-box NDIF gates nothing**: an old client
gets through and fails later, at deserialize, with a message about pickles rather
than versions. Setting `NDIF_MIN_NNSIGHT_VERSION` is the cheapest operational
improvement available to a self-hoster.

Once a minimum is set, three things produce a 400: a missing header (read as an
outdated nnsight), an unparseable version, and a version below the floor. Python
compares major.minor only. The exact messages are in
[Error messages](../reference/error-messages.md#the-client-version-gate).

- Both are read **once at import**. After editing the compose `environment:`
  block, `just up api` recreates the container; `just restart api` restarts the
  process with the old environment.
- An **empty string counts as unset**: `NDIF_MIN_NNSIGHT_VERSION=""` disables the
  gate rather than rejecting everyone.
- An nnsight run from a source tree with no installed distribution reports an
  empty version, which a configured gate rejects as "not provided".
  `pip install -e` the checkout so the distribution metadata exists.

## Pip extras

The core dependency set is small (nnsight, pydantic, redis, click, python-dotenv,
pyyaml, boto3); each role's dependencies are an extra in `pyproject.toml`:

| Extra | Unlocks |
|---|---|
| `api` | `ndif start api` — the HTTP surface. |
| `ray` | A Ray node, the queue dispatcher and the model actors: transformers, accelerate, zstandard for compressed payloads, peft for per-request adapters. |
| `dashboard` | `ndif start dashboard`. Its reconcile cron also needs `ray`; the monitor's probes of PEFT/VLM checkpoints need `peft` (in `ray`) and `torchvision`, which the Dockerfile installs with torch. |
| `metrics` | Loki and InfluxDB shipping. Without it both providers fail open. |
| `postgres` | API-key auth (`asyncpg`). |
| `ext` | Packages a user's traced block may import (datasets, einops, pillow, ...). Not used by NDIF itself. |
| `dev` | The test suite under `tests/`. |

The image installs `[api,ray,metrics,postgres,dashboard]` with `--no-deps`,
because `requirements.txt` already pins every transitive dependency, and then
the `ext` list read back out of `pyproject.toml`. So one image can play any role.

## Checking what a process actually sees

```bash
ndif info                # CLI config, tracked PIDs, endpoint reachability
ndif doctor              # probes redis / rustfs / api / ray from the same vars
docker compose -f docker/docker-compose.yml exec ray ndif info     # inside compose
```

`ndif info` prints the resolved `NDIF_REDIS_URL`, `NDIF_OBJECT_STORE_URL`,
`NDIF_API_URL` and `NDIF_RAY_ADDRESS` with a reachability check for each — the
fastest way to find out a variable you set didn't reach the process.
`NDIF_SERVICE` does double duty: it selects which service(s) `ndif start`
launches *and* becomes the `service` label on every log line and metric point.

## Related

- [Environment Variables](../reference/env-vars.md) — every variable, its default, and its reader.
- [The Compose Stack](compose-stack.md) — the `environment:` blocks in context.
- [Going to Production](production.md) — the minimum set to change before going live.
- [Providers](../developing/providers.md) — how `CONFIG` specs and fail-open behavior are implemented.
- [Enable auth](../runbooks/enable-auth.md) — turning `NDIF_POSTGRES_URL` on.
