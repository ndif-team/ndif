---
title: The ndif CLI
one_liner: Every `ndif` command — service lifecycle, model deploy/scale/evict/restart/export, queue and cluster introspection — with options, defaults, and what each one touches.
tags: [operating, cli, runbook]
related: [docs/operating/models-and-deployment.md, docs/operating/configuration.md, docs/operating/compose-stack.md, docs/operating/quickstart.md, docs/operating/dashboard.md, docs/developing/cli-internals.md, docs/reference/env-vars.md, docs/runbooks/deploy-and-pin-a-model.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/cli/main.py, src/ndif/cli/config.py, src/ndif/cli/service.py, src/ndif/cli/state.py, src/ndif/cli/util.py, src/ndif/cli/commands/deploy.py, src/ndif/cli/commands/start.py, src/ndif/cli/commands/stop.py, src/ndif/cli/commands/doctor.py, src/ndif/cli/commands/version.py, src/ndif/cli/commands/export.py, src/ndif/cli/lib/deploy.py, src/ndif/cli/lib/models.py, src/ndif/cli/lib/model_config.py, src/ndif/cli/lib/events.py, src/ndif/services/api/queue/dispatcher.py, docker/Dockerfile, justfile]
---

# The ndif CLI

## What this covers

`ndif` is the only binary this repo ships (the `ndif` script in `pyproject.toml`
maps to `ndif.cli:cli`). It does three unrelated jobs, and knowing which job a
command belongs to is most of the battle:

1. **Process lifecycle on one host** — `start`, `stop`, `logs`, `info`, `doctor`,
   `version`. With the compose stack you use `just` instead — except *inside* a
   container, where `ndif start` is the entrypoint.
2. **Model control-plane ops** — `deploy`, `scale`, `evict`, `restart`, `status`,
   `export`. They talk to the Ray controller actor, from anywhere that can reach
   `NDIF_RAY_ADDRESS`.
3. **Queue introspection** — `queue`, `kill`. These reach the API's dispatcher
   over a Redis stream, not HTTP.

| Command | Purpose | Talks to |
|---|---|---|
| `ndif start [SERVICES...]` | Start services, detached or foreground | local processes |
| `ndif stop [SERVICES...]` | Stop tracked services, reverse order | local processes |
| `ndif logs SERVICE` | Tail a detached service's captured log | `$NDIF_HOME/logs` |
| `ndif info` | Config, tracked PIDs, endpoint reachability | local state + probes |
| `ndif doctor` | Versions, binaries, GPU, connectivity; exits non-zero on failure | local + probes |
| `ndif version` | The resolved stack: ndif, nnsight, torch (+CUDA), transformers, ray, peft, accelerate, python | installed metadata |
| `ndif deploy [CHECKPOINTS...]` | Place model replicas | Ray controller, Redis |
| `ndif scale CHECKPOINT` | Add replicas matching the ones already running | Ray controller |
| `ndif evict [CHECKPOINTS...]` | Remove model replicas | Ray controller, Redis |
| `ndif restart CHECKPOINT` | Kill + await respawn of a model's replicas | Ray controller + actors |
| `ndif status` | Deployments by level + cluster GPU resources | Ray controller |
| `ndif export` | Dump HOT deployments as `models.yaml` | Ray controller |
| `ndif queue` | Processor status, queue depth, in-flight requests | Redis → dispatcher |
| `ndif kill REQUEST_ID` | Cancel a queued or executing request | Redis → dispatcher |
| `ndif env` | The Ray cluster's Python + package versions | API `/env`, or `--local` |

## Configuration

The group callback loads `./.env`, then any `--env-file` with override, before
any command runs; the full precedence, including `-e` and the typed shortcuts,
is in [Configuration](configuration.md#precedence). The CLI's own floor is
`DEFAULTS` in `src/ndif/cli/config.py` — used by `config.get()` and by the
environment handed to spawned services, never written into `os.environ`:

| Var | CLI default | Note |
|---|---|---|
| `NDIF_HOME` | `~/.ndif` | root for `run/` PID files and `logs/` |
| `NDIF_REDIS_URL` | `redis://localhost:6379` | matches `RedisProvider` |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | matches `ObjectStoreProvider` |
| `NDIF_API_URL` / `NDIF_API_PORT` | `http://localhost:8001` / `8001` | read by `env`, `doctor`, `info` |
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | Ray *client* address |
| `NDIF_RAY_HEAD_PORT` | `6385` | Ray's own default GCS port is 6379 — same as Redis. `start.sh` defaults to 6385 too |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | |

**`NDIF_HOME` state** (`src/ndif/cli/state.py` `State`): `run/<service>.pid`,
`logs/<service>.log`, and `minio/` when the CLI spawns MinIO. Liveness is the PID
file plus `kill(pid, 0)`; a PID file whose process is gone is cleared on read.
Nothing about deployments is stored here — that lives in the controller.

## Service lifecycle

### `ndif start`

`ndif start [SERVICES...] [-e KEY=VALUE]... [--redis-url URL] [--ray-address ADDR] [--ray-head-address HOST:PORT] [--api-port PORT] [--restart] [-f|--foreground]`

| Option | Type / default | Effect |
|---|---|---|
| `SERVICES...` | `$NDIF_SERVICE`, else the core stack | `redis`, `minio`, `ray`, `api`, `dashboard`, or `all` (which expands to the core stack in place) |
| `-e/--env` | `KEY=VALUE`, repeatable | injected into every started service |
| `--redis-url`, `--ray-address`, `--api-port` | str, str, int | set the matching `NDIF_*` var |
| `--ray-head-address` | `HOST:PORT` | sets `NDIF_RAY_HEAD_ADDRESS`; makes this a worker node |
| `--restart` | flag, off | restart already-running services instead of skipping |
| `-f/--foreground` | flag, off | run attached instead of detaching |

With `NDIF_RAY_HEAD_ADDRESS` set the default target becomes **just `ray`** — a
worker node runs nothing else. Otherwise it is the core stack `redis, minio,
ray, api` in dependency order (`src/ndif/cli/service.py` `SERVICES`).
`dashboard` is opt-in (`OPTIONAL_SERVICES`) and never pulled in by a bare
`ndif start`. `NDIF_SERVICE` is split on spaces and commas and `all` expands in
place, so `"ray api"` runs both and `"all dashboard"` is the core stack plus the
dashboard.

`ray`, `api` and `dashboard` are launched by running their own `start.sh`
verbatim — every knob there is env-driven, so the CLI only injects an
environment. `redis` and `minio` are external binaries spawned directly, ports
derived from `NDIF_REDIS_URL` / `NDIF_OBJECT_STORE_URL`; MinIO also gets
`MINIO_ROOT_USER`/`MINIO_ROOT_PASSWORD` from `NDIF_OBJECT_STORE_ACCESS_KEY` /
`_SECRET_KEY`. Detached services get their own session/process group, so each
survives the CLI exiting and can be signalled as a unit. A detached service is
given 2 seconds; one that has already exited is reported `✗ exited immediately`
with its log path instead of getting a PID file.

**As the container entrypoint.** The image's `ENTRYPOINT` is `["ndif"]` and its
`CMD` is `["start", "--foreground"]`, with `NDIF_SERVICE=all` as the image
default — so a bare `docker run ndif/ndif` brings up redis, minio, ray and api
in one container, and `docker run ndif/ndif doctor` runs that command instead.
Compose picks one role per container by setting `NDIF_SERVICE`. In foreground
mode a **single** service replaces the CLI via `execvpe` (it becomes PID 1 and
gets signals directly); **several** run as children with SIGTERM/SIGINT
forwarding, and the first to exit tears the rest down and sets the exit code.
`NDIF_SERVICE` is also the Loki `service=` label, so a multi-service value
appears verbatim in your logs.

### `ndif stop`

`ndif stop [SERVICES...]` — no options. Default target is the core stack
**reversed** (api, ray, minio, redis). SIGTERM to each service's process group,
escalating to SIGKILL after 10s (`src/ndif/cli/util.py` `terminate_pid`), then
the PID file is cleared. For `ray` it then runs `ray stop --force`, because
`ray start` daemonises the GCS server and raylet outside the tracked process
group.

`ndif stop` only knows about processes *it* started. It cannot stop a compose
stack.

### `ndif logs`

`ndif logs SERVICE [-f/--follow] [-n/--lines N]` — `SERVICE` is one of
`redis|minio|ray|api|dashboard`; `--lines` defaults to 100. Shells out to `tail`
on `$NDIF_HOME/logs/<service>.log`. Only detached services have a log file — a
`--foreground` service writes to the terminal, a compose service to docker
(`just logs api`).

### `ndif info`

`ndif info [--json-output]` — prints `NDIF_HOME`, the tracked PID of each
**core** service (the dashboard is not listed), and a reachability probe per
endpoint (`src/ndif/cli/lib/checks.py`): Redis gets a real `PING`, the API
`GET /ping` falling back to a TCP connect, MinIO and Ray TCP connects only.

### `ndif doctor`

`ndif doctor` — no options, read-only; it never installs or changes anything
(`src/ndif/cli/commands/doctor.py`).

| Section | Checks | Counts as failure? |
|---|---|---|
| Environment | Python ≥ 3.12; `ndif` and `nnsight` installed; then `torch` (with its CUDA build), `transformers` and `ray` | Python/ndif/nnsight yes; torch/transformers/ray **no** — reported, not required |
| Binaries | `ray`, `redis-server`, `minio` on `PATH` | yes |
| Compute | `nvidia-smi` returns ≥1 GPU, **and** `torch.cuda.is_available()` is true. The second catches a torch wheel built for a newer CUDA line than the driver supports: nvidia-smi lists the card, torch quietly sees nothing, and Ray would advertise `cuda_memory_bytes: 0` | yes |
| Connectivity | redis / minio / api / ray at their `NDIF_*` URLs | **no** — a stopped service is a normal answer |

Exit code is 1 if any of the first three sections failed. Reach for it first
when `ndif start` fails or a fresh host install misbehaves; it checks *this
host's* PATH, so on a compose-only machine its Binaries section fails by design.
It demands a CUDA GPU: no `nvidia-smi` is a hard failure even though the API and
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
      → conda install --override-channels -c conda-forge minio-server (MinIO no longer publishes standalone binaries)
Compute
  ✓ 2× GPU (NVIDIA A100-SXM4-80GB, 81920 MiB)
  ✓ torch sees 2 GPU(s) (driver CUDA 12.8, torch CUDA 12.6)
Connectivity
  ○ minio not reachable at http://localhost:9000 (start it with `ndif start minio`)

✗ 1 issue found.
```

**Getting a `minio` binary.** MinIO no longer publishes standalone server
binaries (`dl.min.io` returns 410 and the GitHub releases carry no assets). Two
ways that work:

- The conda-forge package, as the hint says. `--override-channels` matters on a
  stock miniconda, which otherwise stops on the default channels'
  terms-of-service prompt:

  ```bash
  conda install --override-channels -c conda-forge minio-server
  ```

- Lift the binary out of the official image, as `docker/Dockerfile` does for the
  published image (its `minio` stage). quay.io, not Docker Hub — the
  `minio/minio` Hub repository no longer resolves:

  ```bash
  cid=$(docker create quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z)
  docker cp "$cid:/usr/bin/minio" /usr/local/bin/minio && docker rm "$cid"
  ```

If you already run an S3-compatible store, point `NDIF_OBJECT_STORE_URL` at it
and skip the binary — this failure does not stop the rest of NDIF.

### `ndif version`

`ndif version [--json-output] [--write PATH]` — the versions that decide how this
server behaves (`src/ndif/cli/commands/version.py` `collect`): `python`, `ndif`,
`nnsight`, `torch`, `transformers`, `ray`, `peft`, `accelerate`, plus `cuda` —
the CUDA line the installed torch wheel was built for. A missing package prints
`(not installed)`; `--write` writes the JSON to `PATH` instead. Distinct from
`ndif --version`, which prints only the `ndif` package version.

The published image runs `ndif version --write /etc/ndif/build.json` at build
time, so `docker run --rm ndif/ndif version` answers "what does this image
carry" without a GPU, a volume, or a port. `ndif env` is the other half:
`version` is about *this* machine, `env` fetches the **cluster's** environment
from the API. Run both to spot client/server drift.

## Model operations

All of these connect via `ensure_ray_connected` (`src/ndif/cli/lib/_common.py`),
which trusts `RayProvider.connected()` — Ray initialized, the address listening,
*and* the controller resolvable — and does one reset-and-reconnect before
failing with `Cannot connect to Ray at <url>`. Each resolves a checkpoint to a
model key, which includes the task: `--task` when given, else inferred from the
checkpoint as an nnsight client does. A deployment made with a non-default task
needs the same `--task` on every later `scale`, `evict` and `restart`.

### `ndif deploy`

`ndif deploy [CHECKPOINTS...] [-f FILE] [--sync] [--revision REV] [--task TASK] [--pinned] [--replicas N] [--actor-class PATH] [--trusted] [--dtype DTYPE] [--gpus N] [--size-bytes N] [--padding-factor F] [--padding-bias N] [--max-tp N] [--ray-address ADDR] [--redis-url URL]`

| Option | Type / default | Effect |
|---|---|---|
| `CHECKPOINTS...` | HF repo ids | one spec each; mutually exclusive with `--sync` |
| `-f/--file` | path | YAML `models:` list (below) |
| `--sync` | flag, off | reconcile the cluster to the file exactly; requires `-f`, forbids positional args |
| `--revision` | str, unset | HF branch/revision; part of the model key |
| `--task` | str, unset | pipeline task (`text-generation`, `fill-mask`, ...); part of the model key, so each task is its own deployment. Inferred when unset |
| `--pinned` | flag, off | exempt from autoscaling / cache eviction |
| `--replicas` | int, `1` | **new** replicas to add per model |
| `--actor-class` | dotted path, default from the controller's env | Ray actor class serving the deployment |
| `--trusted` | flag, off | load with `trust_remote_code=True` |
| `--dtype` | str, unset | a torch dtype, or a quantization (`nf4`/`int4`/`4bit`, `fp4`, `int8`/`8bit`, `fp8`). Loads *and* sizes the model; defaults to `NDIF_DEFAULT_DTYPE` |
| `--gpus` | int, unset | place on exactly this many cards |
| `--size-bytes` | int, unset | the model's weights, measured; skips the Hub estimate |
| `--padding-factor` | float, unset | headroom as a fraction of the model's size |
| `--padding-bias` | int, unset | flat headroom, per model |
| `--max-tp` | int, unset | largest tensor-parallel degree; `0` never places it tensor-parallel |
| `--ray-address` / `--redis-url` | `NDIF_RAY_ADDRESS` / `NDIF_REDIS_URL` | Redis is only for the post-deploy nudge |

What each field does, and what `trusted` changes at load, is in
[Models and Deployment](models-and-deployment.md#per-model-configuration).

**Deploy is additive.** Each call asks the controller for `--replicas` *more*
replicas regardless of what is running; `ndif deploy gpt2` twice gives you two.
Shrinking is `evict`'s job, or `--sync`. One call (`src/ndif/cli/lib/deploy.py`
`deploy`) resolves a model key per spec (`get_model_key`, which canonicalizes
`gpt2` → `openai-community/gpt2` via the Hub, so it needs network access and, for
gated repos, `HF_TOKEN`); with `--sync` reconciles first (`_sync_reconcile`:
evict HOT keys not in the file, trim surplus replicas, reduce each spec to the
shortfall); calls the controller's `_deploy` in two batches, non-pinned then
pinned; then blocks per replica on the actor's `__ray_ready__`, polling every 2s
with **no deadline** (`wait_for_replica_ready`). Only "not registered yet" and
"restarting" are waited through — a constructor that raised propagates as
itself, not as a timeout. Last it sends a fire-and-forget `reconcile_model`
event per touched model key so a live Processor refreshes its replica pool; a
Redis failure there never fails the deploy.

Each model line ends `ready` (or an error); the command exits non-zero only when a model failed outright;
evictions the controller performed to make room are listed at the end.

```console
$ ndif deploy openai-community/gpt2 --replicas 2 --pinned
Generating model key for openai-community/gpt2...
  Model key: nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", "revision": null, "task": "text-generation"}
Connecting to Ray at ray://localhost:10001...

Deploying 1 model(s) (pinned)...
  ⋯ <model_key>: provisioned 2 replica(s), initializing...
  ✓ <model_key> [a1b2c]: ready
  ✓ <model_key> [d3e4f]: ready
```

#### `models.yaml`

`-f` reads a `models:` list whose entries are either a bare checkpoint string or
a mapping (`src/ndif/cli/lib/model_config.py` `load_model_config`):

```yaml
models:
  - openai-community/gpt2
  - checkpoint: meta-llama/Llama-3.1-8B
    revision: main
    task: text-generation
    pinned: true
    replicas: 2
    dtype: bfloat16
    gpus: 4
```

Every field the deploy path understands passes through: `checkpoint`,
`revision`, `task`, `pinned`, `replicas`, `actor_class`, `trusted`, `dtype`,
`padding_factor`, `padding_bias`, `size_bytes`, `gpus`, `max_tp`,
`execution_timeout_seconds`, `envoy_class`, and a precomputed `model_key`. Any
other key is silently dropped. A per-model value overrides the matching CLI flag;
the flag supplies the value for entries that omit it. An entry without `task:`
(and no `--task`) gets the inferred task, as a client would; only a verbatim
`model_key:` must carry the task itself. A missing `models:` key or a non-list value is an
error. The fully annotated example is in
[Models and Deployment](models-and-deployment.md#modelsyaml).

### `ndif scale`

`ndif scale CHECKPOINT [-n N] [--revision REV] [--task TASK] [--actor-class PATH] [--dtype DTYPE] [--gpus N] [--execution-timeout SECONDS] [--trusted] [--pinned] [--ray-address ADDR]`

Adds `-n` replicas (default 1, additive) that look like the ones already serving
that model: unspecified settings are copied from a live replica rather than
taken from the controller's defaults. So growing a tensor-parallel or
non-default-dtype model gives more of the same, not a differently-served replica
under the same key. Anything you pass is used as-is *and* picks which live
replica to copy the rest from — `ndif scale gpt2 --gpus 1` copies the single-GPU
replica. With nothing running it behaves as a plain deploy. The queue's
autoscaler provisions through the same controller call.

### `ndif evict`

`ndif evict [CHECKPOINTS...] [--revision REV] [--task TASK] [--replica ID] [--all] [--ray-address ADDR] [--redis-url URL]`

`--revision` and `--task` apply to *every* checkpoint given. `--replica ID`
targets one replica and requires exactly one checkpoint. `--all` targets every
currently-HOT model key and cannot be combined with checkpoints or `--replica`.
Without `--replica` the controller removes **every HOT and WARM replica** of the
model key. Afterwards the CLI sends the same `reconcile_model` nudge as
`deploy`.

`pinned` protects a deployment from the controller's *automatic* eviction, not
from you — `ndif evict --all` sweeps pinned deployments too. And because `--all`
reads the HOT set from `controller.status()`, a model that is only WARM is not a
target; name it explicitly.

### `ndif restart`

`ndif restart CHECKPOINT [--revision REV] [--task TASK] [--replica ID] [--ray-address ADDR]`
— resolves the model key, asks the controller for the deployment's replicas,
then per replica calls `ray.kill(actor, no_restart=False)` and waits for it to
come back (`src/ndif/cli/lib/restart.py` `_restart_one`). The actor is declared
`max_restarts=-1` so Ray respawns it; the wait is the same `__ray_ready__` poll
as deploy. Use it to drop cached state, reload weights, or recover a wedged
replica without giving up its GPU placement. Unlike `deploy`/`evict` it sends
**no** reconcile event. Unrelated to `ndif start --restart`, which restarts
local *services*.

### `ndif status`

`ndif status [--json-output] [--verbose] [--show-cold] [--watch] [--ray-address ADDR]`

`--json-output` prints the controller payload as JSON; `--verbose` fetches
`controller.get_state()` instead (per-node GPU inventory, per-replica placement,
the evaluator's size cache); `--show-cold` lists COLD models instead of counting
them; `--watch` re-renders every 2s until Ctrl-C.

Levels: **HOT** = on GPU and serving, **WARM** = demoted to CPU cache, **COLD** =
present in the node's HuggingFace cache but not deployed. `application_state` on
a HOT entry comes from Ray's actor state — `RUNNING`, `DEPLOYING`, or
`UNHEALTHY`. The JSON fields are listed in
[Models and Deployment](models-and-deployment.md#evicting-checking-exporting).

### `ndif export`

`ndif export (-f FILE | --stdout) [--ray-address ADDR]` — collapses the current
HOT per-replica list into one entry per model key, counts replicas, and writes a
`models.yaml` you can feed straight back to `ndif deploy -f`. Exactly one of
`-f` / `--stdout` is required. Every live deployment has a concrete `dtype` and
`actor_class`, so entries come out in the mapping form with its `model_key`:

```console
$ ndif export --stdout
models:
- checkpoint: openai-community/gpt2
  dtype: bfloat16
  actor_class: ndif.services.ray.sandbox.model.SandboxModelActor
  model_key: 'nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2",
    "revision": null, "task": "text-generation"}'
```

What survives the round trip, and what does not (`padding_factor`,
`padding_bias`, `size_bytes`, `max_tp`), is in
[Models and Deployment](models-and-deployment.md#evicting-checking-exporting).

## Queue operations

`queue` and `kill` do **not** use HTTP. They append an event to the Redis stream
`dispatcher:events` carrying a unique `response_key`, then block on `BRPOP` for
the dispatcher's JSON reply with a 5s timeout (`src/ndif/cli/lib/events.py`
`_request`). The dispatcher runs as a child of the API's gunicorn master, so
`No response from the dispatcher` means Redis is up but the **API** is not (or
its dispatcher died) — not that Redis is down.

### `ndif queue`

`ndif queue [--json-output] [--watch] [--redis-url URL]` — snapshots every live
Processor: lifecycle status (`uninitialized`, `provisioning`, `deploying`,
`ready`, `cancelled`), the replicas in its pool and whether each is busy, queue
depth, and the queued request ids (first three shown). Processors persist after
their work drains, so an idle cluster still lists one per model that has ever
taken a request, and a processor's `Replicas: N` can be stale for a while after
an eviction — the pool drops evicted replicas lazily, on the next dispatch.

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

`ndif kill REQUEST_ID [--redis-url URL]` — the dispatcher first scans every
Processor's queue and, if the request is still waiting, removes it and answers
the client `ERROR` / "Request cancelled by operator." Otherwise it finds the
replica executing it and cancels the **dispatch**: the client gets the same
`ERROR`, and the dispatcher stops waiting on that replica
(`src/ndif/services/api/queue/dispatcher.py` `Dispatcher._kill`). The model
actor is not interrupted — the block runs to completion, and the client then
also receives the eviction sentence; what a blocking versus a non-blocking user
ends up seeing is in
[Killed jobs](../runbooks/trace-a-users-failed-job.md#killed-jobs). To get the GPU back from a runaway block, follow with
`ndif restart <checkpoint> --replica <id>` (the replica id is in `ndif queue`).
An unknown id exits non-zero with a pointer to `ndif queue`.

### `ndif env`

`ndif env [--json-output] [--all] [--local] [--api-url URL]` — fetches
`GET {NDIF_API_URL}/env` (60s timeout), the Redis-cached snapshot of the Ray
cluster's Python version and installed packages. By default it filters to a
fixed key list (fastapi, uvicorn, gunicorn, redis, ray, nnsight, boto3, torch,
transformers, numpy, pydantic); `--all` prints everything. `--local` reports
this machine's Python, platform, GPUs and a short package list instead.

## Local stack vs. cluster vs. `just`

| Situation | Use |
|---|---|
| Dev on one machine, no docker | `ndif start` / `ndif stop` / `ndif logs` |
| Dev with the compose stack | `just up`, `just logs api`, `just down` — **not** `ndif start` |
| Inside a container | `ndif start --foreground` is already the default command; `NDIF_SERVICE` picks the role(s) |
| Asking an image what it carries | `docker run --rm ndif/ndif version` |
| Adding a GPU worker node | `ndif start --ray-head-address HEAD:6385` (brings up only `ray`) |
| Any cluster, managing models | `ndif deploy` / `scale` / `evict` / `restart` / `status` / `export` |
| Any cluster, debugging traffic | `ndif queue` / `ndif kill` / `ndif env` |

`just` manages *containers*, `ndif start|stop|logs` manage *local processes*;
they track different things and cannot see each other. The model and queue
commands have no `just` equivalent — run them on the host against
`NDIF_RAY_ADDRESS`/`NDIF_REDIS_URL`, or inside a container:
`docker compose -f docker/docker-compose.yml exec ray ndif status`. The host
route needs the same Python and Ray versions as the cluster; the container route
always matches.

## Related

- [Models and Deployment](models-and-deployment.md) — keys, levels, pinning,
  `NDIF_DEPLOYMENTS`, deploy-time `trusted`, what export keeps.
- [Configuration](configuration.md) — the precedence the CLI layers on.
- [The Compose Stack](compose-stack.md), [Quickstart](quickstart.md) — the `just` path.
- [Admin Dashboard](dashboard.md) — the web UI driving the same `cli/lib` functions.
- [CLI Internals](../developing/cli-internals.md) — how the CLI is built and how to add a command.
- [Deploy and pin a model](../runbooks/deploy-and-pin-a-model.md) — the end-to-end recipe.
