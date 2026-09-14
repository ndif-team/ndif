---
title: Quickstart
one_liner: Three routes to your own NDIF — `docker run` from the published image, compose from the checkout, or from source with the `ndif` CLI — each to a working `remote=True` nnsight trace.
tags: [operating, cli, ray, api, auth]
related: [docs/operating/compose-stack.md, docs/operating/configuration.md, docs/operating/production.md, docs/operating/models-and-deployment.md, docs/operating/troubleshooting.md, docs/concepts/request-lifecycle.md, docs/runbooks/enable-auth.md, docs/reference/ports.md, docs/gotchas/networking-and-compose.md]
sources: [justfile, docker/docker-compose.yml, docker/Dockerfile, requirements.txt, pyproject.toml, src/ndif/cli/service.py, src/ndif/cli/commands/start.py, src/ndif/cli/commands/doctor.py, src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/ray/start.sh, tests/conftest.py, tests/test_nnsight_remote.py, .github/workflows/publish_docker.yml]
---

# Quickstart

## What this covers

Standing up your own NDIF on one machine and running a remote nnsight trace
against it. There are three routes, and this page has a section per route:

| Route | Command | Use it when |
|---|---|---|
| **1. `docker run`** | `docker run --gpus all ndif/ndif` | You want an NDIF, not a checkout. One container runs redis, minio, ray and the API. |
| **2. Compose** | `just up` | You are changing this repo, or want Postgres + the telemetry tier alongside. |
| **3. From source** | `ndif start` | No Docker on the box, or you want the services as ordinary host processes. |

All three are designed to come up with **no configuration at all** — every
service has a working single-host default. That convenience has one large
consequence, covered at the bottom: an NDIF with no `NDIF_POSTGRES_URL` is
unauthenticated, and an unauthenticated NDIF runs every request *trusted*.

## Prerequisites

| Requirement | Why | Notes |
|---|---|---|
| An NVIDIA GPU + a CUDA driver | The Ray node computes `cuda_memory_bytes` from torch and the controller only manages nodes reporting a `GPU` resource (`cluster/cluster.py:106`) | Routes 1 and 2 also need the NVIDIA container toolkit |
| Docker with Compose v2 | Routes 1 and 2 | `docker compose version` should print v2.x |
| [`just`](https://github.com/casey/just) | Wraps the compose commands (`justfile:28`) | Route 2 only; optional — every recipe is a one-line `docker compose -f docker/docker-compose.yml ...` |
| Python 3.12 or 3.13 | Route 3 — `requires-python = ">=3.12,<3.14"` (`pyproject.toml:10`), and `ndif doctor` fails below 3.12 (`doctor.py:33-36`) | |
| Disk for model weights | The model actor downloads checkpoints from Hugging Face at deploy time | gpt2 is ~0.5 GB; a 70B checkpoint is ~140 GB |
| Host RAM | Evicted models are held in host RAM as WARM before going COLD | See `NDIF_MODEL_CACHE_PERCENTAGE` in `docs/operating/configuration.md` |

**Without a GPU** the API still answers `GET /ping`, but it cannot serve a model:
`/request`, `/status` and `/env` all depend on `require_ray_connection` and 503
while Ray is unreachable (`src/ndif/services/api/app.py:106-119`). A CPU-only Ray
node doesn't help either — the controller skips any node with no `GPU` resource
(`cluster/cluster.py:106`). NDIF needs at least one GPU node to do work.

## Route 1: `docker run` — the published image

One container runs the whole request path. `NDIF_SERVICE` defaults to `all` in
the image (`Dockerfile:34`), and `all` resolves to the core stack — **redis,
minio, ray, api** (`src/ndif/cli/service.py:66`, `:96-98`). The dashboard is
opt-in and not included (`service.py:75`); `all` expands in place inside a list,
so `NDIF_SERVICE="all dashboard"` adds it.

```bash
docker run --gpus all --shm-size 4g \
  -p 8001:8001 -p 9000:9000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  ndif/ndif:0.1.0
```

- **`--shm-size 4g`**: Ray's object store lives in `/dev/shm`, and Docker's
  64 MB default makes Ray fall back to `/tmp` with a performance warning.

- **8001** is the API — the only port a client posts to.
- **9000** is MinIO's S3 API. Publish it: a result over
  `NDIF_MAX_SOCKET_RESULT_BYTES` comes back as a presigned URL the *client*
  fetches, signed with `NDIF_OBJECT_STORE_PUBLIC_URL` (which falls back to
  `NDIF_OBJECT_STORE_URL`, `http://localhost:9000` by default —
  `providers/objectstore.py:80-82`). Unpublished, large results fail to download.
- The **HF cache mount** is what makes weights survive the container. The image
  declares `VOLUME ["/root/.cache/huggingface"]` (`Dockerfile:137`), so an
  anonymous volume is created if you don't bind one — bind your own and a
  checkpoint downloaded once is reused everywhere.
- `--gpus all` needs the NVIDIA container toolkit.

**Tags.** The torch wheel is chosen at build time from a CUDA line, so there is
one image per line (`.github/workflows/publish_docker.yml:44-47`, `:83-86`):

| Tag | What |
|---|---|
| `ndif/ndif:0.1.0-cu126` | torch from the cu126 index — the default line |
| `ndif/ndif:0.1.0-cu130` | torch from the cu130 index |
| `ndif/ndif:0.1.0` | the same image as `0.1.0-cu126` |
| `ndif/ndif:latest` | the same image as `0.1.0-cu126` |

Any 12.x wheel runs on any 12.x driver ≥ 525 (CUDA minor version compatibility),
so `cu126` is the wide default; take `cu130` for a newer driver you want the
matching wheel for. A wheel built for a CUDA line your driver predates does not
fail loudly — `torch.cuda.is_available()` just returns `False`
(`docker/Dockerfile:52-57`).

**Other commands.** `ENTRYPOINT` is `ndif` and `CMD` is `start --foreground`
(`Dockerfile:141-142`), so replacing the command runs any other CLI command:

```bash
docker run --rm ndif/ndif:0.1.0 version          # what this image actually carries
docker run --rm --gpus all ndif/ndif:0.1.0 doctor
docker run --rm -e NDIF_SERVICE=api ndif/ndif:0.1.0   # one service (what compose does)
```

The build also records its resolved versions at `/etc/ndif/build.json`
(`Dockerfile:111`) and its build inputs as OCI labels (`Dockerfile:118-128`), so a
tagged image can always say what it is.

**Deploy a model** into a running container the same way as anywhere else:

```bash
docker exec <container> ndif deploy openai-community/gpt2
docker exec <container> ndif status
```

Or skip it — the queue is lazy and the first remote trace of a model deploys it.

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
`NDIF_SERVICE`. The build installs torch (its own layer, from the `TORCH_CUDA`
build arg, `cu126` by default) and then `requirements.txt` (Ray, transformers,
nnsight from PyPI), so expect ten minutes or more the first time. Later builds
reuse those layers.

nnsight is a normal installed dependency, baked into the image
(`nnsight>=0.8.0rc1,<0.9`, `requirements.txt:25`). For client-side development,
`just up`/`just ta` additionally bind-mount an **editable** nnsight checkout over
the image's copy (`docker/docker-compose.nnsight.yml`, resolved from
`NNSIGHT_PATH`, `justfile:25-28`), so changes to your working tree are picked up
without a rebuild — `pip install -e /path/to/nnsight` in the shell you run `just`
from, or set `NNSIGHT_PATH` yourself. A **non-editable** nnsight (one under
site-packages) is deliberately not mounted, and no nnsight at all skips the mount;
either way the image's own pinned copy is used. `just nnsight` prints which it
will be.

## Route 3: From source, with the `ndif` CLI

No Docker. The CLI spawns each service as a host process and tracks it by PID
file under `NDIF_HOME` (`~/.ndif`).

From PyPI, no checkout:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu126   # first
pip install "ndif[api,ray,metrics,postgres,dashboard]"
```

From a checkout, when you want the exact pinned set the image is built from:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu126   # before requirements.txt
pip install -r requirements.txt
pip install ".[api,ray,metrics,postgres,dashboard]"
```

torch first either way: nnsight pulls torch in, and with no wheel installed yet
pip would take PyPI's default, which is the CUDA 13 build. `pyproject.toml`
pins `nnsight>=0.8.0rc1,<0.9` itself, so a plain `pip install ndif` gets a 0.8
server; the sdist also ships `requirements.txt` and `docs/` if you want them
without cloning.

torch is deliberately **not** in `requirements.txt` — the right wheel is a
property of your driver, not of this repo (`requirements.txt:38-39`). Pick the
index that matches it, as above.

```bash
ndif doctor        # versions, binaries, GPU, connectivity — read this before starting
ndif start         # redis, minio, ray, api, detached
ndif info          # tracked PIDs + reachability
ndif logs api -f
ndif stop
```

`ndif start` with no arguments brings up the core stack in dependency order
(`src/ndif/cli/service.py:66`); `ndif start dashboard` adds the admin UI, which a
bare `ndif start` never pulls in (`service.py:75`).

**The two binaries `ndif doctor` checks for** (`doctor.py:56-69`):

- **`redis-server`** — your package manager, or conda-forge. Straightforward.
- **`minio`** — awkward. MinIO no longer publishes standalone server binaries:
  `dl.min.io` returns 410 and the GitHub releases carry no assets, so doctor's
  hint names the conda-forge package. Two options: `conda install
  --override-channels -c conda-forge minio-server` (verified 2026-09-14 on a
  fresh Python 3.12 env; `--override-channels` because a stock miniconda
  otherwise stops on the anaconda channels' terms-of-service prompt), or lift
  the binary out of the official image,
  which is exactly what `docker/Dockerfile:26-27`, `:50` does for the published
  image:

  ```bash
  cid=$(docker create quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z)
  docker cp "$cid:/usr/bin/minio" /usr/local/bin/minio
  docker rm "$cid"
  ```

  quay.io, not Docker Hub: the `minio/minio` Hub repository no longer resolves.
  If you already have an S3-compatible store, you don't need the binary at all —
  point `NDIF_OBJECT_STORE_URL` at it and start the other three services
  (`ndif start redis ray api`).

`ndif start` is also how a worker node joins an existing head: set
`NDIF_RAY_HEAD_ADDRESS` (or `--ray-head-address HOST:PORT`) and the default
target narrows to `ray` alone (`cli/commands/start.py:134`). See
`docs/runbooks/add-a-gpu-node.md`.

## Check it's healthy

The checks below are the same whichever route you took; only the way you list
the processes differs (`just ps`, `docker ps`, or `ndif info`).

```bash
just ps            # route 2
```

You want `redis`, `minio`, `influxdb` and `postgres` reporting `(healthy)` — the
API's `depends_on` blocks on exactly those health checks
(`docker-compose.yml:177-185`) — plus `api` `(healthy)` on its own `/ping` check
(`docker-compose.yml:171-176`) and `ray`, `dashboard`, `loki`, `grafana` and
`prometheus` `running`.

```bash
curl localhost:8001/ping            # "pong" — the API process is alive
curl localhost:8001/connected       # {"status":"connected"} once Ray is reachable
curl -s localhost:8001/status       # cluster status blob (503 while Ray is down)
```

`/ping` (`app.py:324`) only proves the web process is up. `/connected`
(`app.py:331-335`) is the real readiness signal: it passes once the queue dispatcher
has connected to Ray and set the `ray:connected` flag in Redis. Ray is the slow
one — it boots CUDA, computes this node's resources
(`src/ndif/services/ray/resources.py`), starts a head, and launches the
controller (`src/ndif/services/ray/start.sh:50-70`).

```bash
just logs api      # follow one service (Ctrl-C detaches; the service keeps running)
just logs ray
```

In `just logs ray` you are looking for `Starting Ray head node with resources:
{"head": 10, "cuda_memory_bytes": ..., "cpu_memory_bytes": ...}` followed by
`Starting NDIF controller...`. A `cuda_memory_bytes` of `0` means torch can't
see a GPU inside the container — the container toolkit isn't wired up.

Other useful surfaces, all published to the host by the dev compose (route 2 —
`docker run` publishes only what you pass `-p` for):

| URL | What |
|---|---|
| http://localhost:8081 | Admin dashboard (no login — dev mode is on) |
| http://localhost:8265 | Ray dashboard: actors, nodes, logs |
| http://localhost:3000 | Grafana (anonymous admin), lands on the NDIF Overview |
| http://localhost:9001 | MinIO console (`minioadmin` / `minioadmin`) |

## Deploy a first model

You usually don't have to. The queue is lazy: a request for a model with no live
replica provisions one on the spot (`src/ndif/services/api/queue/processor.py:1-20`),
so the first remote trace of `openai-community/gpt2` deploys it and then runs.
The client just sits in `QUEUED`/`DEPLOYING` while the weights download.

To deploy ahead of time, run the CLI where it can reach Ray. Under compose that
is the `ray` container; under `docker run` it is the one container; from source
it is just your shell:

```bash
docker compose -f docker/docker-compose.yml exec ray ndif deploy openai-community/gpt2   # route 2
docker exec <container> ndif deploy openai-community/gpt2                                 # route 1
ndif deploy openai-community/gpt2                                                         # route 3
```

Two other routes: set `NDIF_DEPLOYMENTS` (a `|`-separated list of model keys) on
the `ray` service to deploy on controller start
(`deployments/controller/controller.py:108-109` — they are deployed **pinned**),
or use the dashboard's deploy button. See `docs/operating/models-and-deployment.md` for revisions, dtypes,
pinning and `models.yaml`.

## Run a remote trace

Same for all three routes. On the client side, install nnsight and point it at
your server:

```bash
pip install "nnsight>=0.8.0rc1"     # 0.8 is a pre-release; the specifier names it
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

The model is never dispatched locally — the client only needs the architecture to
build the request; the server owns the weights. No API key is needed because auth
is off by default.

This is exactly what the repo's own suite does. `tests/conftest.py:33` sets the
same host, `:53-63` skips everything if `GET /ping` doesn't answer, and
`tests/test_nnsight_remote.py:29-32` runs the trace above and asserts
`hidden.shape[-1] == 768`. Once the stack is up:

```bash
pip install -e ".[dev]"
pytest tests/
```

The workflows in `.github/workflows/` build and publish images and the PyPI
package; none of them run tests. Bringing the stack up and running pytest is the
whole test story. See `docs/developing/testing.md`.

## What just happened

Five steps, each covered in `docs/concepts/request-lifecycle.md`:

- The client serialized the traced block plus its inputs and `POST`ed it to
  `/request` (`app.py:122`). `validate_request` parsed the envelope and — auth
  being off and the request not saying otherwise — defaulted it to
  `trusted = True` (`api/auth.py:180-184`).
- The API pushed the request onto a Redis list (`NDIF_QUEUE_KEY`, default
  `queue`) and returned; the client opened a `/subscribe` websocket and started
  receiving status updates published to its session channel.
- The dispatcher popped it, handed it to the per-model `Processor`, and the
  Processor asked the Ray controller for a replica — deploying gpt2 if one
  wasn't already HOT.
- The model actor ran the traced block against the real weights and uploaded the
  saved values to MinIO, publishing a presigned download URL on the `COMPLETED`
  response. That URL is signed with `NDIF_OBJECT_STORE_PUBLIC_URL`
  (`http://localhost:9000` in compose, and the same by default in the all-in-one
  container) so your *client*, not the server, can fetch it.
- The client downloaded the blob, deserialized it, and `hidden` appeared in your
  local frame.

## Before you expose this to anyone

**None of the three routes sets `NDIF_POSTGRES_URL`, so none of them has
authentication, and no authentication means every request runs trusted.** The
line is commented out in the compose file (`docker-compose.yml:165`) and unset
everywhere else, so `PostgresProvider.enabled()` is False, `verify_api_key`
returns `None`, and `validate_request` defaults `request.trusted` to `True` for
any caller that doesn't set it (`api/auth.py:180-184`). A client that sends
`trusted: false` is honored, which is the one way to exercise the sandbox path
with no Postgres.

This is the intended default for an NDIF you run for yourself. It is the wrong
default the moment a second person can reach the port.

`trusted` is not a soft label. It decides two things:

- The traced block runs **in-process inside the model actor**, next to the
  weights, instead of in a separate runner process
  (`src/ndif/services/ray/sandbox/model.py`). Whatever Python a caller submits
  executes with the model actor's privileges.
- The flag rides into `trust_remote_code=` when the model is sized and loaded
  (`cluster/cluster.py:183`, `:225`, `:296`), so a request for an arbitrary
  Hugging Face repo can execute that repo's code on your GPU node.

On top of that, the dev compose publishes Redis, Postgres, the Ray client port
and the Ray dashboard to the host with no credentials, and the admin dashboard
runs with `NDIF_DASHBOARD_DEV_MODE=true` (`docker-compose.yml:216`), which
bypasses its login entirely (`dashboard/backend/auth.py:73`).

Before anything but you can reach port 8001: work through
`docs/runbooks/enable-auth.md` (set `NDIF_POSTGRES_URL`, create keys, grant the
`trusted` tag only to keys you actually trust) and then
`docs/operating/production.md` for the rest — dashboard credentials, real
object-store URLs, and what the server does *not* provide (no TLS, no rate
limiting).

## Gotchas

- **`just down` removes containers,** but downloaded model weights survive — the
  `ray` service bind-mounts the host HF cache. `just down -v` additionally drops
  the dashboard's state volume.
- **`docker run` without a volume for `/root/.cache/huggingface`** gets an
  anonymous one from the image's `VOLUME` (`Dockerfile:137`), so weights survive
  a restart of that container but not a `docker rm`. Bind the host cache.
- **Publish 9000 as well as 8001** on the `docker run` route, or any result over
  `NDIF_MAX_SOCKET_RESULT_BYTES` completes server-side and then fails to
  download client-side.
- **After a code change, `just up` is not enough** — the image is stale. Use
  `just ta` (down → build → up), or `just ta ray` for one service. (nnsight is the
  exception: its dev bind-mount picks up client changes without a rebuild.)
- **Ray takes minutes to become ready.** A 503 from `/request` right after
  `just up` usually just means the controller isn't up yet; check `just logs ray`
  before assuming anything is broken.
- **Gated checkpoints need `HF_TOKEN`.** The compose `ray` service passes it
  through from your environment (`HF_TOKEN: ${HF_TOKEN:-}`), so export it before
  `just up`.

## Related

- `docs/operating/compose-stack.md` — what each of those ten containers is for.
- `docs/operating/configuration.md` — how the env-only config model layers.
- `docs/operating/models-and-deployment.md` — deploying, pinning, evicting.
- `docs/operating/troubleshooting.md` — symptom → diagnosis → fix.
- `docs/concepts/request-lifecycle.md` — the five bullets above, in detail.
- `docs/runbooks/enable-auth.md` — turning on API-key auth.
