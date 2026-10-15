---
title: Quickstart
one_liner: Three routes to your own NDIF — `docker run` from the published image, compose from the checkout, or from source with the `ndif` CLI — each to a working `remote=True` nnsight trace.
tags: [operating, cli, ray, api, auth]
related: [docs/operating/compose-stack.md, docs/operating/configuration.md, docs/operating/production.md, docs/operating/models-and-deployment.md, docs/operating/cli.md, docs/operating/troubleshooting.md, docs/concepts/request-lifecycle.md, docs/concepts/auth-and-limits.md, docs/runbooks/enable-auth.md, docs/reference/ports.md, docs/developing/testing.md, docker/README.md]
sources: [justfile, docker/docker-compose.yml, docker/Dockerfile, requirements.txt, pyproject.toml, src/ndif/cli/service.py, src/ndif/cli/commands/start.py, src/ndif/cli/commands/doctor.py, src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/ray/start.sh]
---

# Quickstart

## What this covers

Standing up your own NDIF on one machine and running a remote nnsight trace
against it. There are three routes, and this page has a section per route:

| Route | Command | Use it when |
|---|---|---|
| **1. `docker run`** | `docker run --gpus all ndif/ndif` | You want an NDIF, not a checkout. One container runs redis, rustfs, ray and the API. |
| **2. Compose** | `just up` | You are changing this repo, or want Postgres + the telemetry tier alongside. |
| **3. From source** | `ndif start` | No Docker on the box, or you want the services as ordinary host processes. |

All three come up with **no configuration at all** — every service has a
working single-host default. Read the next section before you choose how to
publish ports.

## Before you expose this to anyone

**None of the three routes sets `NDIF_POSTGRES_URL`, so none of them has
authentication.** With auth off, `verify_api_key` returns `None` and
`validate_request` honors a client-supplied `trusted` and defaults an
unspecified one to true (both in `src/ndif/services/api/auth.py`). A default
nnsight client sends none, so in practice every request runs trusted:

- The traced block runs **in-process inside the model actor**, next to the
  weights, with the actor's privileges — not in a separate runner process.
- The flag becomes `trust_remote_code=` when a model is sized and loaded, so a
  request naming an arbitrary Hugging Face repo can run that repo's code on your
  GPU node.

That is the intended default for an NDIF only you can reach, and the wrong one
the moment anyone else can. Two more things are open by default:

- **The compose stack publishes its backing services on every interface** with
  default or no credentials: Redis (6379), Postgres (5432), RustFS (9000/9001),
  the Ray client port (10001), the Ray dashboard (8265), InfluxDB, Loki,
  Prometheus and Grafana. Anyone who reaches Redis or 10001 can run code on the
  cluster. On a machine others can reach, change each `ports:` entry to a
  `127.0.0.1:` bind (`"127.0.0.1:6379:6379"`) or firewall them.
- **The compose dashboard runs with `NDIF_DASHBOARD_DEV_MODE: "true"`**, which
  skips its login entirely, and it can deploy and evict models.

On the `docker run` route, publish with `127.0.0.1:` binds (as below) until
you have done the hardening. Before anything but you can reach port 8001, work
through [Enable auth](../runbooks/enable-auth.md) and then
[Going to Production](production.md). How `trusted` is decided, and what
changes for a key without the tag, is in
[Auth and Limits](../concepts/auth-and-limits.md).

## Prerequisites

| Requirement | Why | Notes |
|---|---|---|
| An NVIDIA GPU + a CUDA driver | The controller manages only nodes reporting a `GPU` resource (`src/ndif/services/ray/deployments/controller/cluster/cluster.py` `Cluster.update_nodes`) | Routes 1 and 2 also need the NVIDIA container toolkit, registered as a Docker runtime: if containers see no GPU, run `sudo nvidia-ctk runtime configure --runtime=docker` and restart Docker |
| Docker with Compose v2 | Routes 1 and 2 | `docker compose version` should print v2.x |
| [`just`](https://github.com/casey/just) | Wraps the compose commands | Route 2 only. Install: `curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh \| bash -s -- --to ~/.local/bin`, with `~/.local/bin` on `PATH`. Every recipe is a one-line `docker compose -f docker/docker-compose.yml ...` |
| Python 3.12 or 3.13 | Route 3: `requires-python = ">=3.12,<3.14"` in `pyproject.toml`; `ndif doctor` fails below 3.12 | |
| Disk for the image | Routes 1 and 2: the image is ~8 GB, and a compose build adds ~8 GB of build cache | Both land under Docker's data root (typically `/var/lib/docker`); check free space there, not only on the weights disk |
| Disk for model weights | The model actor downloads checkpoints from Hugging Face at deploy time | gpt2 is ~0.5 GB; a 70B checkpoint is ~140 GB |
| Host RAM | Evicted models are held in host RAM as WARM before going COLD | `NDIF_MODEL_CACHE_PERCENTAGE` |

**Without a GPU** the API still answers `GET /ping`, but `/request`, `/status`
and `/env` 503 while Ray is unreachable (`src/ndif/services/api/app.py`
`require_ray_connection`), and a CPU-only Ray node is skipped by the
controller. NDIF needs at least one GPU node to do work.

## Route 1: `docker run` — the published image

One container runs the whole request path. `NDIF_SERVICE` defaults to `all` in
the image, which resolves to the core stack — **redis, rustfs, ray, api**
(`src/ndif/cli/service.py` `SERVICES`). The dashboard is opt-in
(`OPTIONAL_SERVICES`); `NDIF_SERVICE="all dashboard"` adds it.

```bash
docker run -d --name ndif --gpus all --shm-size 4g \
  -p 127.0.0.1:8001:8001 -p 127.0.0.1:9000:9000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -e HF_TOKEN \
  ndif/ndif:0.1.0
docker logs -f ndif
```

- **`--shm-size 4g`**: Ray's object store lives in `/dev/shm`, and Docker's
  64 MB default is far too small.
- **8001** is the API — the only port a client posts to.
- **9000** is RustFS's S3 API. Publish it: a result too large to inline comes
  back as a presigned URL the *client* fetches, signed with
  `NDIF_OBJECT_STORE_PUBLIC_URL` (default `http://localhost:9000`). Unpublished,
  large results fail to download. See
  [Status and Results](../concepts/status-and-results.md) for the cap.
- The **HF cache mount** is what makes weights survive the container. The image
  declares `VOLUME ["/root/.cache/huggingface"]`, so without a bind you get an
  anonymous volume that a `docker rm` discards.
- **`-e HF_TOKEN`** passes your token through for gated checkpoints.
- **`--user $(id -u):$(id -g)`** (optional) runs the stack as your uid, so the
  weights it writes to the mounted cache are yours rather than root's. The image
  sets `HOME=/root` and makes `/root` writable by any uid, so the mount path is
  the same for every uid. Run `mkdir -p ~/.cache/huggingface` first, or Docker
  creates it root-owned. Only the dashboard's cron jobs need root.

Image tags (one per CUDA line) and how to pick one are in
[docker/README.md](../../docker/README.md#tags). A torch wheel built for a CUDA
line newer than your driver does not fail loudly — `torch.cuda.is_available()`
just returns `False`, which `ndif doctor` reports.

The image's `ENTRYPOINT` is `ndif` and its `CMD` is `start --foreground`, so
replacing the command runs any other CLI command:

```bash
docker run --rm ndif/ndif:0.1.0 version          # what this image actually carries
docker run --rm --gpus all ndif/ndif:0.1.0 doctor
```

## Route 2: Docker Compose — the development stack

Each service in its own container, next to Postgres and the full telemetry tier
(Loki, InfluxDB, Prometheus, Grafana). Built from this checkout, so it is the
route for changing NDIF itself.

```bash
git clone https://github.com/ndif-team/ndif.git
cd ndif
just up            # builds the image on first run, then starts everything detached
```

The first `just up` builds one image (`docker/Dockerfile`) and runs it three
times — as `api`, `ray` and `dashboard` — selected per container by
`NDIF_SERVICE`. The build installs torch (from the `TORCH_CUDA` build arg,
`cu126` by default) and then `requirements.txt`, so expect ten minutes or more
the first time. If you have an editable nnsight installed, `just up` also
bind-mounts it over the image's copy; see
[The Compose Stack](compose-stack.md#just-is-the-interface).

## Route 3: From source, with the `ndif` CLI

No Docker. The CLI spawns each service as a host process and tracks it by PID
file under `NDIF_HOME` (`~/.ndif`).

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu126   # first
pip install "ndif[api,ray,metrics,postgres,dashboard]"
```

From a checkout, for the exact pinned set the image is built from, replace the
second line with `pip install -r requirements.txt && pip install
".[api,ray,metrics,postgres,dashboard]"`. Install torch first either way:
nnsight pulls torch in, and with no wheel installed pip takes PyPI's default,
which is the CUDA 13 build. torch is deliberately not in `requirements.txt` —
the right wheel is a property of your driver.

```bash
ndif doctor        # versions, binaries, GPU, connectivity — read this before starting
ndif start         # redis, rustfs, ray, api, detached
ndif info          # tracked PIDs + reachability
ndif logs api -f
ndif stop
```

`ndif start` brings up the core stack in dependency order; `ndif start
dashboard` adds the admin UI. `ndif doctor` checks for `ray`, `redis-server` and
`rustfs` on `PATH`. `redis-server` comes from your package manager or
conda-forge; getting a `rustfs` binary is covered in
[The ndif CLI](cli.md#ndif-doctor). With an S3-compatible store of your own,
point `NDIF_OBJECT_STORE_URL` at it and run `ndif start redis ray api`.

`ndif start` is also how a worker node joins an existing head: set
`NDIF_RAY_HEAD_ADDRESS` and the default target narrows to `ray` alone. See
[Add a GPU node](../runbooks/add-a-gpu-node.md).

## Check it's healthy

```bash
just ps                             # route 2; `docker ps` / `ndif info` for 1 and 3
curl localhost:8001/ping            # "pong" — the API process is alive
curl localhost:8001/connected       # {"status":"connected"} once Ray is reachable
```

Under compose you want `redis`, `rustfs`, `influxdb` and `postgres` reporting
`(healthy)` — the `api` service's `depends_on` waits on exactly those — plus
`api` `(healthy)` on its own `/ping` check, and the rest `running`.

`/ping` only proves the web process is up. `/connected` is the readiness
signal: it passes once the queue dispatcher has connected to Ray and set the
`ray:connected` flag in Redis. Ray is the slow one — it boots CUDA, computes
this node's resources, starts a head, and launches the controller. In its log
(`just logs ray`, `docker logs ndif`, or `ndif logs ray`) look for
`Starting Ray head node with resources: {"head": 10, "cuda_memory_bytes": ...}`
then `Starting NDIF controller...`. A `cuda_memory_bytes` of `0` means torch
can't see a GPU — under Docker, the container toolkit isn't wired up.

The compose stack also publishes these (route 1 publishes only what you pass
`-p` for):

| URL | What |
|---|---|
| http://localhost:8081 | Admin dashboard (no login — dev mode is on) |
| http://localhost:8265 | Ray dashboard: actors, nodes, logs |
| http://localhost:3000 | Grafana (anonymous admin), lands on the NDIF Overview |
| http://localhost:9001/rustfs/console/ | RustFS console (`rustfsadmin` / `rustfsadmin`) |

## Deploy a first model

You usually don't have to. The queue is lazy: a request for a model with no
live replica provisions one on the spot, so the first remote trace of
`openai-community/gpt2` deploys it and then runs. The client sits in
`PROVISIONING`/`DEPLOYING` while the weights download.

To deploy ahead of time, run the CLI where it can reach Ray:

```bash
docker exec ndif ndif deploy openai-community/gpt2                                       # route 1
docker compose -f docker/docker-compose.yml exec ray ndif deploy openai-community/gpt2  # route 2
ndif deploy openai-community/gpt2                                                        # route 3
```

Revisions, tasks, dtypes, pinning, `models.yaml` and deploy-on-boot are in
[Models and Deployment](models-and-deployment.md).

## Run a remote trace

Same for all three routes. Install nnsight on the client and point it at your
server:

```bash
pip install nnsight
```

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:8001"

from nnsight.modeling.transformers import TransformersModel
model = TransformersModel("openai-community/gpt2", task="text-generation")

with model.trace("The Eiffel Tower is in the city of", remote=True):
    hidden = model.transformer.h[-1].output.save()

print(hidden.shape)   # torch.Size([1, 10, 768])
```

The model is never dispatched locally — the client only needs the architecture
to build the request; the server owns the weights. No API key is needed because
auth is off.

What happened: the client `POST`ed the serialized block to `/request`, the API
queued it in Redis, the dispatcher handed it to a gpt2 replica (deploying one
first), and the result came back over the `/subscribe` websocket or a presigned
RustFS URL. [Request Lifecycle](../concepts/request-lifecycle.md) walks each hop.

The repo's own test suite runs this same trace against `localhost:8001`; see
[Testing](../developing/testing.md).

### Client on another machine

With the ports bound to `127.0.0.1`, reach the API over SSH from the client
machine:

```bash
ssh -fN -L 8001:localhost:8001 user@server     # the API
ssh -fN -L 9000:localhost:9000 user@server     # the object store
```

The second tunnel is not optional. Every non-blocking result, and any blocking
result over the inline cap, is downloaded by the client from a presigned URL
built on `NDIF_OBJECT_STORE_PUBLIC_URL`, so that address must resolve from the
client. A presigned URL's signature covers its host, so the client must reach the
store under exactly that host and port (`localhost:9000` through the tunnel
above, for the default). What a wrong address looks like is in
[Error Messages](../reference/error-messages.md).

## Gotchas

- **`just down` removes containers,** but downloaded weights survive in the
  host HF cache the `ray` service bind-mounts. The Postgres database, RustFS
  blobs and telemetry do not; see
  [The Compose Stack](compose-stack.md#persistence).
- **After a code change, `just up` is not enough** — the source is baked into
  the image. Use `just ta` (down → build → up). `just ta ray` still takes the
  whole stack down and then starts only `ray` and its dependencies; run
  `just up` afterwards to bring the rest back.
- **Ray takes minutes to become ready.** A 503 from `/request` right after
  startup usually means the controller isn't up yet; check the ray log before
  assuming anything is broken.
- **Gated checkpoints need `HF_TOKEN`.** The compose `ray` service passes it
  through from your environment, so export it before `just up`.
- **A port already in use on the host** fails that service's start. Edit its
  `ports:` entry in `docker/docker-compose.yml`; the justfile passes `-f`
  explicitly, so a `docker-compose.override.yml` is not read.
- **The compose HF cache is a fixed path,** `${HOME}/.cache/huggingface`. The
  `ray` container runs as root unless `NDIF_USER` is set
  (`NDIF_USER=$(id -u):$(id -g) just up`), so by default the files it
  downloads are root-owned. A cache root already wrote to needs
  `sudo chown -R $(id -u):$(id -g) ~/.cache/huggingface` once before running as
  yourself. On a cache shared with other work every cached model shows `COLD`
  in `/status`.

## Related

- [The Compose Stack](compose-stack.md) — what each of the ten containers is for.
- [Configuration](configuration.md) — how settings reach each process.
- [Models and Deployment](models-and-deployment.md) — deploying, pinning, evicting.
- [Troubleshooting](troubleshooting.md) — the "something broke" entry point.
- [Request Lifecycle](../concepts/request-lifecycle.md) — the trace above, hop by hop.
- [Enable auth](../runbooks/enable-auth.md) — turning on API-key auth.
