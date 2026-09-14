---
title: The ndif CLI
one_liner: Every `ndif` command — service lifecycle, model deploy/evict/restart, queue and cluster introspection — with options, defaults, and what each one touches.
tags: [operating, cli, runbook]
related: [docs/operating/models-and-deployment.md, docs/operating/configuration.md, docs/operating/compose-stack.md, docs/operating/quickstart.md, docs/operating/dashboard.md, docs/developing/cli-internals.md, docs/reference/env-vars.md, docs/runbooks/deploy-and-pin-a-model.md]
sources: [src/ndif/cli/main.py, src/ndif/cli/config.py, src/ndif/cli/service.py, src/ndif/cli/state.py, src/ndif/cli/commands/deploy.py, src/ndif/cli/commands/start.py, src/ndif/cli/commands/doctor.py, src/ndif/cli/commands/version.py, src/ndif/cli/lib/deploy.py, src/ndif/cli/lib/events.py, docker/Dockerfile, justfile]
---

# The ndif CLI

## What this covers

`ndif` is the only binary this repo ships (`pyproject.toml:126-127` maps it to
`ndif.cli:cli`; `python -m ndif.cli` is equivalent). It does three unrelated jobs, and
knowing which job a command belongs to is most of the battle:

1. **Process lifecycle on one host** — `start`, `stop`, `logs`, `info`, `doctor`,
   `version`. With
   the compose stack you use `just` instead — except *inside* a container, where
   `ndif start` is the entrypoint.
2. **Model control-plane ops** — `deploy`, `evict`, `restart`, `status`, `export`. They
   talk to the Ray controller actor, from anywhere that can reach `NDIF_RAY_ADDRESS`.
3. **Queue introspection** — `queue`, `kill`. These reach the API's dispatcher over a
   Redis stream, not HTTP.

| Command | Purpose | Talks to |
|---|---|---|
| `ndif start [SERVICES...]` | Start services, detached or foreground | local processes |
| `ndif stop [SERVICES...]` | Stop tracked services, reverse order | local processes |
| `ndif logs SERVICE` | Tail a detached service's captured log | `$NDIF_HOME/logs` |
| `ndif info` | Config, tracked PIDs, endpoint reachability | local state + probes |
| `ndif doctor` | Versions, binaries, GPU, connectivity; exits non-zero on failure | local + probes |
| `ndif version` | The resolved stack: ndif, nnsight, torch (+CUDA), transformers, ray, peft, accelerate, python | installed metadata |
| `ndif deploy [CHECKPOINTS...]` | Place model replicas | Ray controller, Redis |
| `ndif evict [CHECKPOINTS...]` | Remove model replicas | Ray controller, Redis |
| `ndif restart CHECKPOINT` | Kill + await respawn of a model's replicas | Ray controller + actors |
| `ndif status` | Deployments by level + cluster GPU resources | Ray controller |
| `ndif export` | Dump HOT deployments as `models.yaml` | Ray controller |
| `ndif queue` | Processor status, queue depth, in-flight requests | Redis → dispatcher |
| `ndif kill REQUEST_ID` | Cancel a queued or executing request | Redis → dispatcher |
| `ndif env` | The Ray cluster's Python + package versions | API `/env`, or `--local` |

## Configuration: `--env-file`, `.env`, defaults

The group callback loads env files before any command runs (`src/ndif/cli/main.py:41`
→ `src/ndif/cli/config.py:42`):

```python
load_dotenv(Path.cwd() / ".env")
if env_file:
    load_dotenv(env_file, override=True)
```

Precedence, highest first: **`--env-file`** (loaded with `override=True`, so it beats
even your shell) → the real process environment → **`./.env`** in the current
directory (no override, so it only fills gaps) → `config.DEFAULTS` (`src/ndif/cli/config.py:22-31`), a
floor used by `config.get()` and by the environment handed to spawned services, never
written into `os.environ`. `DEFAULTS` exists because service defaults are tuned for
docker's per-container network, or collide on one host:

| Var | CLI default | Note |
|---|---|---|
| `NDIF_HOME` | `~/.ndif` | root for `run/` PID files and `logs/` |
| `NDIF_REDIS_URL` | `redis://localhost:6379` | matches `RedisProvider` |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | matches `ObjectStoreProvider` |
| `NDIF_API_URL` / `NDIF_API_PORT` | `http://localhost:8001` / `8001` | read by `env`, `doctor`, `info` |
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | Ray *client* address |
| `NDIF_RAY_HEAD_PORT` | `6385` | Ray's own default GCS port is 6379 — same as Redis |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | |

> **Gotcha:** `NDIF_RAY_HEAD_PORT` defaults to `6385` both in the CLI's `DEFAULTS`
> and in `services/ray/start.sh`, deliberately offset from Redis's 6379 (which is
> also Ray's *native* GCS default). Because both paths agree, the head port is
> unambiguous however Ray is launched. If you override it, set the same value
> everywhere the head and its workers read it.

`build_env` (`src/ndif/cli/config.py:54`) assembles a spawned service's environment as `DEFAULTS` →
real environment → `-e KEY=VALUE` pairs → typed shortcuts (`--redis-url`,
`--ray-address`, `--ray-head-address`, `--api-port`).

**`NDIF_HOME` state** (`src/ndif/cli/state.py:19`): `run/<service>.pid`,
`logs/<service>.log`, and `minio/` when the CLI spawns MinIO. Liveness is the PID file
plus `kill(pid, 0)`; a PID file whose process is gone is cleared lazily on read
(`state.py:55`). Nothing about deployments is stored here — that lives in the controller.

## Service lifecycle

### `ndif start`

`ndif start [SERVICES...] [-e KEY=VALUE]... [--redis-url URL] [--ray-address ADDR] [--ray-head-address HOST:PORT] [--api-port PORT] [--restart] [-f|--foreground]`

| Option | Type / default | Effect |
|---|---|---|
| `SERVICES...` | `$NDIF_SERVICE`, else all | `redis`, `minio`, `ray`, `api`, `dashboard`, or `all` (which expands to the core stack in place) |
| `-e/--env` | `KEY=VALUE`, repeatable | injected into every started service |
| `--redis-url`, `--ray-address`, `--api-port` | str, str, int | set the matching `NDIF_*` var |
| `--ray-head-address` | `HOST:PORT` | sets `NDIF_RAY_HEAD_ADDRESS`; makes this a worker node |
| `--restart` | flag, off | restart already-running services instead of skipping |
| `-f/--foreground` | flag, off | run attached instead of detaching |

Target resolution (`src/ndif/cli/commands/start.py:134`): with `NDIF_RAY_HEAD_ADDRESS`
set the default becomes **just `ray`** — a worker node runs nothing else. Otherwise it
is the core stack `redis, minio, ray, api` in dependency order (`service.py:66`).
`dashboard` is opt-in and never pulled in by a bare `ndif start` (`service.py:75`).

`ray` and `api` are launched by running their own `start.sh` verbatim — every knob
there is env-driven, so the CLI only injects an environment. `redis` and `minio` are
external binaries spawned directly, ports derived from `NDIF_REDIS_URL` /
`NDIF_OBJECT_STORE_URL` (`service.py:29`, `:35`); MinIO also gets
`MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` from `NDIF_OBJECT_STORE_ACCESS_KEY`/`_SECRET_KEY`.
Detached services get `start_new_session=True`, so each is its own session/process
group, survives the CLI exiting, and can be signalled as a unit.

```console
$ ndif start
  ✓ redis: started (pid 41233) → /home/you/.ndif/logs/redis.log
  • ray: already running (pid 41240), skipping
```

**As the container entrypoint.** The image's `ENTRYPOINT` is `["ndif"]` and its
`CMD` is `["start", "--foreground"]` (`docker/Dockerfile:138-139`), with
`NDIF_SERVICE=all` as the image default (`Dockerfile:34`) — so a bare
`docker run ndif/ndif` brings up redis, minio, ray and api in one container, and
`docker run ndif/ndif doctor` (or `version`) runs that command instead. Compose
picks one role per container by setting `NDIF_SERVICE` to `api`, `ray`, or
`dashboard` (`docker/docker-compose.yml:147`, `:233`, `:196`). `env_services()`
splits it on spaces and commas (`service.py:83-85`), and `resolve_targets`
expands `all` in place (`service.py:88-98`) — so `NDIF_SERVICE="ray api"` runs
both, `all` (or empty) means the core stack, and `"all dashboard"` means the core
stack plus the dashboard.
In foreground mode a **single** service replaces the CLI via `execvpe` (it becomes
PID 1 and gets signals directly); **several** run as children with SIGTERM/SIGINT
forwarding, and the first to exit tears the rest down and sets the exit code
(`start.py:52`).

> **Gotcha:** `NDIF_SERVICE` is dual-purpose — `api/start.sh` and `ray/start.sh` also
> export it as the Loki `service=` label, so a multi-service value appears verbatim in
> your logs.

### `ndif stop`

`ndif stop [SERVICES...]` — no options. Default target is the core stack **reversed**
(api, ray, minio, redis). SIGTERM to the process group, escalating to SIGKILL after 10s
(`src/ndif/cli/util.py:51`), then the PID file is cleared.

> **Gotcha:** `ndif stop` only knows about processes *it* started. It cannot stop a
> compose stack, and won't reap a Ray head you launched by hand.

### `ndif logs`

`ndif logs SERVICE [-f/--follow] [-n/--lines N]` — `SERVICE` is one of
`redis|minio|ray|api|dashboard`; `--lines` defaults to 100. Shells out to `tail` on
`$NDIF_HOME/logs/<service>.log`. Only detached services have a log file — a
`--foreground` service writes to the terminal, a compose service to docker
(`just logs api`).

### `ndif info`

`ndif info [--json-output]` — prints `NDIF_HOME`, the tracked PID of each **core**
service (the dashboard is not listed even when tracked), and a reachability probe per
endpoint. Probes never raise (`src/ndif/cli/lib/checks.py`): Redis gets a real `PING`,
the API `GET /ping` falling back to a TCP connect, MinIO and Ray TCP connects only.

```console
$ ndif info
Home: /home/you/.ndif
Services:
  redis: 🟢 running (pid 41233)     minio: ⚪ stopped
Connectivity:
  ✓ redis redis://localhost:6379    ✗ minio http://localhost:9000
```

### `ndif doctor`

`ndif doctor` — no options, read-only; it never installs or changes anything
(`src/ndif/cli/commands/doctor.py`).

| Section | Checks | Counts as failure? |
|---|---|---|
| Environment | Python ≥ 3.12; `ndif` and `nnsight` installed; then `torch` (with its CUDA build), `transformers` and `ray`, read through `version.collect()` (`doctor.py:46-52`) | Python/ndif/nnsight yes; torch/transformers/ray **no** — they are reported, not required |
| Binaries | `ray`, `redis-server`, `minio` on `PATH` (`doctor.py:56-69`) | yes |
| Compute | `nvidia-smi` returns ≥1 GPU, **and** `torch.cuda.is_available()` is true. The second catches a torch wheel built for a newer CUDA line than the driver supports (a `-cu130` image on a 12.x driver): nvidia-smi lists the card, torch quietly sees nothing, and Ray would advertise `cuda_memory_bytes: 0` | yes |
| Connectivity | redis / minio / api / ray at their `NDIF_*` URLs | **no** |

Connectivity is informational by design — a stopped service is a normal answer
(`doctor.py:92-104`). Exit code is 1 if any of the first three sections failed.
Reach for it first when `ndif start` fails or a fresh install misbehaves. Note it
demands a CUDA GPU: no `nvidia-smi` is a hard failure even though the API and
Redis start fine without one.

```console
$ ndif doctor
Environment
  ✓ Python 3.12.14
  ✓ ndif 0.1.0
  ✓ nnsight 0.8.0rc1
  ✓ torch 2.14.0+cu126 (CUDA 12.6)
  ✓ transformers 5.17.0
  ✓ ray 2.55.1
Binaries
  ✓ ray → /usr/local/bin/ray
  ✓ redis-server → /usr/bin/redis-server
  ✗ minio not on PATH
      → conda install -c conda-forge minio-server (MinIO no longer publishes standalone binaries)
Compute
  ✓ 2× GPU (NVIDIA A100-SXM4-80GB, 81920 MiB)
  ✓ torch sees 2 GPU(s) (driver CUDA 12.8, torch CUDA 12.6)
Connectivity
  ○ minio not reachable at http://localhost:9000 (start it with `ndif start minio`)

✗ 1 issue found.
```

> **Gotcha: the `minio` hint has nowhere to send you.** MinIO no longer publishes
> standalone server binaries — `dl.min.io` returns 410 and the GitHub releases
> carry no assets — so "install the MinIO server binary" is not an actionable
> instruction any more. The options are the conda-forge `minio-server` package
> (unverified here), or extracting the binary from the official image the way the
> Dockerfile does (`docker/Dockerfile:26-27`, `:50`):
>
> ```bash
> cid=$(docker create quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z)
> docker cp "$cid:/usr/bin/minio" /usr/local/bin/minio && docker rm "$cid"
> ```
>
> quay.io, not Docker Hub: `minio/minio` on the Hub no longer resolves. If you
> already run an S3-compatible store, point `NDIF_OBJECT_STORE_URL` at it and
> ignore the binary — this failure does not stop the rest of NDIF.

### `ndif version`

`ndif version [--json-output] [--write PATH]` — the versions that decide how this
server behaves (`src/ndif/cli/commands/version.py`). Distinct from
`ndif --version`, which prints only the `ndif` package version (`main.py:31`).

| Option | Effect |
|---|---|
| _(none)_ | aligned `key  value` lines; a missing package prints `(not installed)` |
| `--json-output` | the same dict as JSON |
| `--write PATH` | writes the JSON to `PATH` (creating parent dirs) and prints `wrote <path>` instead |

Reported: `python`, then `ndif`, `nnsight`, `torch`, `transformers`, `ray`,
`peft`, `accelerate` (`version.py:19`), plus `cuda` — `torch.version.cuda`, the
CUDA line the installed torch wheel was built for, added only when torch imports
(`version.py:30-36`).

```console
$ ndif version
python        3.12.4
ndif          0.1.0
nnsight       0.8.0rc1
torch         2.13.0
transformers  5.15.0
ray           2.55.1
peft          0.19.1
accelerate    1.14.0
cuda          12.6
```

The published image runs `ndif version --write /etc/ndif/build.json` at build
time (`docker/Dockerfile:108`), so a tagged image records what it resolved to
even before you start it, and `docker run --rm ndif/ndif version` answers the
question without a GPU, a volume, or a port.

`ndif env` is the other half of this: `version` is about *this* machine, `env`
fetches the **cluster's** environment from the API. Run both to spot client/server
drift.

## Model operations

All of these connect via `ensure_ray_connected` (`src/ndif/cli/lib/_common.py:23`),
which trusts `RayProvider.connected()` — Ray initialized, the address listening, *and*
the `Controller` actor resolvable — and does one reset-and-reconnect before failing
with `Cannot connect to Ray at <url>`.

### `ndif deploy`

`ndif deploy [CHECKPOINTS...] [-f FILE] [--sync] [--revision REV] [--pinned] [--replicas N] [--actor-class PATH] [--trusted] [--dtype DTYPE] [--gpus N] [--size-bytes N] [--padding-factor F] [--padding-bias N] [--max-tp N] [--ray-address ADDR] [--redis-url URL]`

| Option | Type / default | Effect |
|---|---|---|
| `CHECKPOINTS...` | HF repo ids | one spec each; mutually exclusive with `--sync` |
| `-f/--file` | path | YAML `models:` list (below) |
| `--sync` | flag, off | reconcile the cluster to the file exactly; requires `-f`, forbids positional args |
| `--revision` | str, unset | HF branch/revision; part of the model key |
| `--pinned` | flag, off | exempt from autoscaling / cache eviction |
| `--replicas` | int, `1` | **new** replicas to add per model |
| `--actor-class` | dotted path, default `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | Ray actor class serving the deployment |
| `--trusted` | flag, off | load with `trust_remote_code=True`; see below |
| `--dtype` | str, unset | how the weights are held — a torch dtype, or a quantization (`nf4`/`int4`/`4bit`, `fp4`, `int8`/`8bit`, `fp8`). Loads *and* sizes the model; defaults to the controller's `NDIF_DEFAULT_DTYPE` |
| `--gpus` | int, unset | place on exactly this many cards instead of deriving the count from the size |
| `--size-bytes` | int, unset | the model's weights, measured; skips the Hub estimate |
| `--padding-factor` | float, unset | headroom as a fraction of the model's size |
| `--padding-bias` | int, unset | flat headroom, per model |
| `--max-tp` | int, unset | largest tensor-parallel degree; `0` never places it tensor-parallel |
| `--ray-address` / `--redis-url` | `NDIF_RAY_ADDRESS` / `NDIF_REDIS_URL` | Redis is only for the post-deploy nudge |

**Deploy is additive.** Each call asks the controller for `--replicas` *more* replicas
regardless of what is running (`src/ndif/cli/lib/deploy.py:1`); running
`ndif deploy gpt2` twice gives you two. Shrinking is `evict`'s job, or `--sync`. One
call does:

1. **Resolve a model key** per spec by constructing the nnsight wrapper on meta (no
   weights) and calling `to_model_key()` (`lib/models.py:16`). The default wrapper
   `nnsight.modeling.transformers.TransformersModel` prefixes the key. This also
   canonicalizes `gpt2` → `openai-community/gpt2` via the Hub, so deploy needs network
   access and, for gated repos, `HF_TOKEN`.
2. **Connect to Ray**, grab the `Controller` actor.
3. With `--sync`, reconcile first (`lib/deploy.py:196`): evict every HOT model key not
   in the file, trim surplus replicas per model, reduce each remaining spec to the
   shortfall only.
4. **Call `controller._deploy`** in two batches, non-pinned then pinned
   (`lib/deploy.py:101`), with a `DeploymentConfig` per model key.
5. **Block per replica** on the actor's `__ray_ready__`, polling every 2s with no
   deadline (`lib/models.py`). A model that must download weights sits here a
   while, which is why there is no deadline: only two failures mean "not yet"
   (the actor is unregistered, or restarting) and everything else — including a
   constructor that raised — propagates as itself instead of as a timeout.
6. **Nudge the dispatcher**: fire-and-forget `reconcile_model` events on Redis for every
   touched model key, so an already-live Processor refreshes its replica pool.
   Best-effort — a Redis failure never fails a deploy that already succeeded.

Per-model status ends up `READY`, `PARTIAL` (some replicas failed) or `ERROR`;
evictions the controller performed to make room are listed at the end.

```console
$ ndif deploy openai-community/gpt2 --replicas 2 --pinned
Generating model key for openai-community/gpt2...
  Model key: nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", ...}
Connecting to Ray at ray://localhost:10001...

Deploying 1 model(s) (pinned)...
  ⋯ <model_key>: provisioned 2 replica(s), initializing...
  ✓ <model_key> [a1b2c]: ready
  ✓ <model_key> [d3e4f]: ready
```

#### `models.yaml`

`-f` reads a `models:` list whose entries are either a bare checkpoint string or a
mapping (`src/ndif/cli/lib/model_config.py`):

```yaml
models:
  - openai-community/gpt2
  - checkpoint: meta-llama/Llama-3.1-8B
    revision: main
    pinned: true
    replicas: 2
    trusted: false
    dtype: bfloat16
    padding_factor: 0.15
    gpus: 4
    max_tp: 8
    execution_timeout_seconds: 3600
    envoy_class: ndif.services.ray.deployments.modeling.base.ModelActor
    actor_class: ndif.services.ray.deployments.modeling.base.ModelActor
    model_key: null
```

The loader passes through every field the deploy path understands
(`cli/lib/model_config.py`): `checkpoint`, `revision`, `pinned`, `replicas`,
`actor_class`, `trusted`, `dtype`, `padding_factor`, `padding_bias`, `size_bytes`,
`gpus`, `max_tp`, `execution_timeout_seconds`,
`envoy_class`, and a precomputed `model_key`. A per-model value overrides the
matching CLI flag default (`--revision`, `--pinned`, `--replicas`, `--actor-class`,
`--trusted`, `--dtype`) for entries that omit it. A missing `models:` key or a
non-list value is an error. Pair `-f` with `ndif export` to snapshot and restore a
cluster.

#### `trusted`

`trusted` is a per-deployment flag; `ndif deploy --trusted` sets it and
`models.yaml` accepts a `trusted:` key (`src/ndif/cli/lib/deploy.py:42`, applied at
`:113`). It is not a label — it changes two things:

- **How the weights load.** It becomes `DeploymentConfig.trusted`, which the controller
  passes as `trust_remote_code=` to the size evaluator and to the actor's model load
  (`services/ray/deployments/controller/cluster/cluster.py:183`, `:225`, `:296`). Deploying a checkpoint
  whose HF repo ships custom modelling code with `trusted=False` fails to load.
- **Where user code runs.** The same flag on a *request* — stamped by the API from the
  API key's `trusted` user_tag, or, when auth is off, honored from the request and
  defaulting to `True` when the client leaves it unspecified
  (`services/api/auth.py:180-184`) — decides whether the traced block runs in-process
  inside the model actor or in a separate runner subprocess
  (`services/ray/sandbox/model.py:207-222`). Isolation here is process-based, and it is
  still in progress.

So `ndif deploy --trusted` (or `trusted: true` in `models.yaml`) can deploy a
`trust_remote_code` model directly. Dashboard-initiated deploys still default
`trusted: True` as an admin action
(`services/dashboard/backend/routers/deployments.py:36-38`).

### `ndif scale`

`ndif scale CHECKPOINT [-n N] [--revision REV] [--actor-class PATH] [--dtype DTYPE] [--gpus N] [--execution-timeout SECONDS] [--trusted] [--pinned] [--ray-address ADDR]`

| Option | Type / default | Effect |
|---|---|---|
| `CHECKPOINT` | HF repo id | the model to grow (exactly one) |
| `-n` | int, `1` | how many replicas to **add** — additive, like `deploy --replicas` |
| `--actor-class` / `--dtype` / `--gpus` / `--execution-timeout` | — | override instead of matching what is running |
| `--trusted` / `--pinned` | flags, off | as in `deploy` |

Adds replicas that look like the ones already serving that model. The only
difference from `deploy --replicas` is where the *unspecified* settings come
from: `deploy` uses the controller's defaults, `scale` copies a live replica.

That matters when a model is served differently from how the controller would
choose on its own — a tensor-parallel replica, or a non-default dtype. Growing it
with `deploy` gives a replica placed by the defaults, so two replicas under one
model key answer differently and nothing says which one you got. `scale` gives
you more of what is there.

Anything you pass is used as-is *and* decides which live replica counts as a
match to copy the rest from, so `ndif scale gpt2 --gpus 1` copies from the
single-GPU replica rather than the sharded one.

```bash
ndif scale meta-llama/Llama-3.2-1B          # one more, sharded like the rest
ndif scale meta-llama/Llama-3.2-1B -n 2
```

**With nothing running there is nothing to copy**, and this behaves as a plain
deploy — at cold start no live replica claims the model is served any particular
way, so the evaluator decides. A durable answer to that belongs in `models.yaml`.

The queue's autoscaler provisions through the same call
(`services/api/queue/replica.py`), which is the point: it has no idea how a model
is served and should not be deciding.

### `ndif evict`

`ndif evict [CHECKPOINTS...] [--revision REV] [--replica ID] [--all] [--ray-address ADDR] [--redis-url URL]`

`--revision` applies to *every* checkpoint given. `--replica ID` targets one replica and
requires exactly one checkpoint. `--all` targets every currently-HOT model key and
cannot be combined with checkpoints or `--replica`. Without `--replica` the controller
removes **every HOT and WARM replica** of the model key: `node.evict` frees the GPU
memory and demotes HOT→WARM where CPU headroom allows, and the fan-out drains both
levels, so one `ndif evict gpt2` really does clear it. Afterwards the CLI sends the same
`reconcile_model` nudge as `deploy`.

```console
$ ndif evict openai-community/gpt2
Connecting to Ray at ray://localhost:10001...
  Model key for openai-community/gpt2: nnsight...:{"repo_id": ...}
Evicting from 1 model(s)...
  ✓ <model_key>: evicted 2 replica(s)
      - [a1b2c] 1 GPU(s), 0.2478 GB
```

The trailing "Evicted N replica(s) across M model(s) / Total GPUs freed" summary only
prints for multi-model or multi-replica evictions.

> **Gotcha:** `pinned` protects a deployment from the controller's *automatic* eviction
> (`node.evictable`), not from you — `ndif evict --all` sweeps pinned deployments too.
> And because `--all` reads the HOT set from `controller.status()`, a model that is only
> WARM is not a target; name it explicitly.

### `ndif restart`

`ndif restart CHECKPOINT [--revision REV] [--replica ID] [--ray-address ADDR]` —
resolves the model key, asks the controller for the deployment's replicas, then per
replica calls `ray.kill(actor, no_restart=False)` and waits for it to come back
(`src/ndif/cli/lib/restart.py:69`). The actor is declared `max_restarts=-1` so Ray
respawns it; the wait is the same `__ray_ready__` poll as deploy. Use it to drop
cached state, reload weights, or recover a wedged replica without giving up its GPU
placement. Unlike `deploy`/`evict` it sends **no** reconcile event. Unrelated to
`ndif start --restart`, which restarts local *services*.

### `ndif status`

`ndif status [--json-output] [--verbose] [--show-cold] [--watch] [--ray-address ADDR]`

`--json-output` prints the controller payload as JSON; `--verbose` fetches
`controller.get_state()` instead of `controller.status()` and prints that (per-node GPU
inventory, per-replica placement, the evaluator's size cache); `--show-cold` lists COLD
models instead of counting them; `--watch` re-renders every 2s until Ctrl-C.

Levels: **HOT** = on GPU and serving, **WARM** = demoted to CPU cache, **COLD** =
present in the node's HuggingFace cache but not deployed (`controller.status()` fills
COLD from `get_downloaded_models()`). `application_state` on a HOT entry comes from
Ray's actor state — `RUNNING`, `DEPLOYING`, or `UNHEALTHY`.

```console
$ ndif status
NDIF Cluster Status
============================================================
Cluster Resources:
  Nodes: 1 | Total GPUs: 2 | GPU Memory: 61.3 / 79.1 GB free

Active Deployments:
  🔥 HOT (1)
    • openai-community/gpt2
      RUNNING | 124M params
  🌡️  WARM (0)
    (none)
  ❄️  COLD (3)
    (use --show-cold to list all 3 models)
```

### `ndif export`

`ndif export (-f FILE | --stdout) [--ray-address ADDR]` — collapses the current HOT
per-replica list into one entry per model key, counts replicas, and writes a
`models.yaml` you can feed straight back to `ndif deploy -f`. Exactly one of `-f` /
`--stdout` is required. An entry with no revision, not pinned, one replica and no actor
class is written in the short string form:

```console
$ ndif export --stdout
models:
- openai-community/gpt2
- checkpoint: meta-llama/Llama-3.1-8B
  pinned: true
  replicas: 2
```

## Queue operations

`queue` and `kill` do **not** use HTTP. They append an event to the Redis stream
`dispatcher:events` carrying a unique `response_key`, then block on `BRPOP` for the
dispatcher's JSON reply with a 5s timeout (`src/ndif/cli/lib/events.py:33`). The
dispatcher runs inside the API's gunicorn master.

> **Gotcha:** `No response from the dispatcher` means Redis is up but the **API** is not
> (or its dispatcher died) — not that Redis is down.

### `ndif queue`

`ndif queue [--json-output] [--watch] [--redis-url URL]` — snapshots every live
Processor: lifecycle status (`uninitialized`, `provisioning`, `deploying`, `ready`,
`cancelled`), the replicas in its pool and whether each is busy, queue depth, and the
queued request ids (first three shown). A model with no in-flight work has no Processor,
so an idle cluster prints `No active processors.` even with HOT deployments.

```console
$ ndif queue
NDIF Queue Status
============================================================
Overview:
  Active processors: 1 | Queued requests: 2 | Executing: 1

  openai-community/gpt2
    Status: READY
    Replicas: 1
    Queue depth: 2
    ⚙ [a1b2c] executing 5f3e9d2a (for 0:00:07)
    Queued: 9c1d4b, 7b2a08
```

### `ndif kill`

`ndif kill REQUEST_ID [--redis-url URL]` — the dispatcher first scans every Processor's
queue and removes the request if it is still waiting (answering the client
`Status.ERROR` / "Request cancelled by operator."); otherwise it finds the replica
executing it and cancels there. An unknown id exits non-zero with a pointer to
`ndif queue`.

### `ndif env`

`ndif env [--json-output] [--all] [--local] [--api-url URL]` — fetches
`GET {NDIF_API_URL}/env` (60s timeout), the Redis-cached snapshot of the Ray cluster's
Python version and installed packages. By default it filters to a fixed key list
(fastapi, uvicorn, gunicorn, redis, ray, nnsight, boto3, torch, transformers, numpy,
pydantic); `--all` prints everything. `--local` skips the API and reports this machine's
Python, platform, GPUs and a short package list — the pair is the fastest way to spot
client/server `nnsight` drift.

## Local stack vs. cluster vs. `just`

| Situation | Use |
|---|---|
| Dev on one machine, no docker | `ndif start` / `ndif stop` / `ndif logs` |
| Dev with the compose stack | `just up`, `just logs api`, `just down` — **not** `ndif start` |
| Inside a container | `ndif start --foreground` is already the default command; `NDIF_SERVICE` picks the role(s) |
| Asking an image what it carries | `docker run --rm ndif/ndif version` |
| Adding a GPU worker node | `ndif start --ray-head-address HEAD:6385` (brings up only `ray`) |
| Any cluster, managing models | `ndif deploy` / `evict` / `restart` / `status` / `export` |
| Any cluster, debugging traffic | `ndif queue` / `ndif kill` / `ndif env` |

`just` (`justfile`) is a thin wrapper over `docker compose -f docker/docker-compose.yml`
and overlaps only the lifecycle half: `just up|down|restart|logs|ps` manage
*containers*, `ndif start|stop|logs` manage *local processes*. They track different
things and cannot see each other. The model and queue commands have no `just`
equivalent — run them on the host against `NDIF_RAY_ADDRESS`/`NDIF_REDIS_URL`, or as
`docker compose exec api ndif status`. In compose, `redis` and `minio` come from
upstream images (`docker/docker-compose.yml:18`, `:127`), so `ndif start redis|minio` is
a single-host convenience needing those binaries on `PATH` — exactly what `ndif doctor`
checks. The published image carries both itself (apt's `redis-server`, and the
`minio` binary copied out of the quay.io image — `docker/Dockerfile:46-50`), which
is what makes `NDIF_SERVICE=all` possible in one container.

## Related

- `docs/operating/models-and-deployment.md` — levels, pinning, `NDIF_DEPLOYMENTS`, and
  what a model key actually means.
- `docs/operating/configuration.md` — the full env-var story the CLI layers on.
- `docs/operating/compose-stack.md`, `docs/operating/quickstart.md` — the `just` path.
- `docs/operating/dashboard.md` — the web UI driving deploy/evict/status through the
  same `cli/lib` functions.
- `docs/developing/cli-internals.md` — how the CLI is built and how to add a command.
- `docs/reference/env-vars.md` — every `NDIF_*` variable and its default.
- `docs/runbooks/deploy-and-pin-a-model.md` — the end-to-end recipe.
