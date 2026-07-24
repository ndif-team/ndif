---
title: Quickstart
one_liner: From a clean checkout to a working `remote=True` nnsight trace against your own NDIF, in one `just up`.
tags: [operating, cli, ray, api, auth]
related: [docs/operating/compose-stack.md, docs/operating/configuration.md, docs/operating/production.md, docs/operating/models-and-deployment.md, docs/operating/troubleshooting.md, docs/concepts/request-lifecycle.md, docs/runbooks/enable-auth.md, docs/reference/ports.md, docs/gotchas/networking-and-compose.md]
sources: [justfile, docker/docker-compose.yml, docker/Dockerfile, src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/ray/start.sh, tests/conftest.py, tests/test_nnsight_remote.py]
---

# Quickstart

## What this covers

Standing up your own NDIF on one machine and running a remote nnsight trace
against it. The whole stack is a docker compose file wrapped by `just`, and it
is designed to come up with **no configuration at all** — every service has a
working single-host default. That convenience has one large consequence, covered
at the bottom: a bare `just up` is unauthenticated, and an unauthenticated NDIF
runs every request *trusted*.

## Prerequisites

| Requirement | Why | Notes |
|---|---|---|
| Docker with Compose v2 | The whole stack is `docker/docker-compose.yml` | `docker compose version` should print v2.x |
| [`just`](https://github.com/casey/just) | Wraps the compose commands (`justfile:14`) | Optional — every recipe is a one-line `docker compose -f docker/docker-compose.yml ...` |
| An NVIDIA GPU + the NVIDIA container toolkit | The `ray` service reserves `driver: nvidia, count: all` (`docker-compose.yml:257-263`) | Without it the `ray` container fails to start |
| Disk for model weights | The model actor downloads checkpoints from Hugging Face at deploy time | gpt2 is ~0.5 GB; a 70B checkpoint is ~140 GB |
| Host RAM | Evicted models are held in host RAM as WARM before going COLD | See `NDIF_MODEL_CACHE_PERCENTAGE` in `docs/operating/configuration.md` |

> **Note:** the compose `ray` service bind-mounts the host Hugging Face cache
> (`${HOME}/.cache/huggingface` → `/root/.cache/huggingface`) and passes
> `HF_TOKEN` through, so downloaded weights persist across `just down` and gated
> models work if the token is in your environment.

**Without a GPU** you can still bring up everything except `ray`
(`just up api redis minio loki influxdb grafana prometheus`) and the API will
answer `GET /ping`. It cannot serve a model: `/request`, `/status` and `/env`
all depend on `require_ray_connection` and 503 while Ray is unreachable
(`src/ndif/services/api/app.py:106-119`). A CPU-only Ray node doesn't help
either — the controller only manages nodes that report a `GPU` resource
(`cluster/cluster.py:91-92`). NDIF needs at least one GPU node to do work.

## Bring it up

```bash
git clone https://github.com/ndif-team/ndif.git
cd ndif
just up            # builds the image on first run, then starts everything detached
```

The first `just up` builds one image (`docker/Dockerfile`) and runs it three
times — as `api`, `ray` and `dashboard` — selected per container by
`NDIF_SERVICE`. The build installs a multi-GB pinned dependency set
(`requirements.txt`: torch cu124, Ray, transformers, nnsight), so expect ten
minutes or more the first time. Later builds reuse that layer.

nnsight is a normal installed dependency, baked into the image. For client-side
development, `just up`/`just ta` additionally bind-mount a local editable nnsight
over the image's copy (`docker/docker-compose.nnsight.yml`, resolved from
`NNSIGHT_PATH`), so changes to your working checkout are picked up without a
rebuild — install nnsight editable (`pip install -e /path/to/nnsight`) so it
resolves to your source. If nnsight isn't importable the mount is skipped and the
image's own copy is used.

## Check it's healthy

```bash
just ps
```

You want `redis`, `minio`, `influxdb`, `loki` and `postgres` reporting
`(healthy)` — the API's `depends_on` blocks on exactly those health checks
(`docker-compose.yml:159-167`) — plus `api`, `ray`, `dashboard`, `grafana` and
`prometheus` `running`.

```bash
curl localhost:8001/ping            # "pong" — the API process is alive
curl localhost:8001/connected       # {"status":"connected"} once Ray is reachable
curl -s localhost:8001/status       # cluster status blob (503 while Ray is down)
```

`/ping` (`app.py:321`) only proves the web process is up. `/connected`
(`app.py:327`) is the real readiness signal: it passes once the queue dispatcher
has connected to Ray and set the `ray:connected` flag in Redis. Ray is the slow
one — it boots CUDA, computes this node's resources
(`src/ndif/services/ray/resources.py`), starts a head, and launches the
controller (`src/ndif/services/ray/start.sh:58-70`).

```bash
just logs api      # follow one service (Ctrl-C detaches; the service keeps running)
just logs ray
```

In `just logs ray` you are looking for `Starting Ray head node with resources:
{"head": 10, "cuda_memory_bytes": ..., "cpu_memory_bytes": ...}` followed by
`Starting NDIF controller...`. A `cuda_memory_bytes` of `0` means torch can't
see a GPU inside the container — the container toolkit isn't wired up.

Other useful surfaces, all published to the host by the dev compose:

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

To deploy ahead of time, run the CLI inside the `ray` container (it needs a Ray
connection, and the container already has one):

```bash
docker compose -f docker/docker-compose.yml exec ray ndif deploy openai-community/gpt2
docker compose -f docker/docker-compose.yml exec ray ndif status
```

Two other routes: set `NDIF_DEPLOYMENTS` (a `|`-separated list of model keys) on
the `ray` service to deploy on controller start
(`deployments/controller/controller.py:533`), or use the dashboard's deploy
button. See `docs/operating/models-and-deployment.md` for revisions, dtypes,
pinning and `models.yaml`.

## Run a remote trace

On the client side, install nnsight and point it at your server. The host is the
only thing that changes versus using ndif.us:

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

The model is never dispatched locally — the client only needs the architecture to
build the request; the server owns the weights. No API key is needed because auth
is off by default.

This is exactly what the repo's own suite does. `tests/conftest.py:21-27` sets
the same host, skips everything if `GET /ping` doesn't answer, and the tests in
`tests/test_nnsight_remote.py` run the trace above and assert
`hidden.shape[-1] == 768`. Once the stack is up:

```bash
pip install -e ".[dev]"
pytest tests/
```

There is no CI in this repo — bringing the stack up and running pytest is the
whole test story. See `docs/developing/testing.md`.

## What just happened

Five steps, each covered in `docs/concepts/request-lifecycle.md`:

- The client serialized the traced block plus its inputs and `POST`ed it to
  `/request` (`app.py:122`). `validate_request` parsed the envelope and — auth
  being off and the request not saying otherwise — defaulted it to
  `trusted = True` (`api/auth.py:180`).
- The API pushed the request onto a Redis list (`NDIF_QUEUE_KEY`, default
  `queue`) and returned; the client opened a `/subscribe` websocket and started
  receiving status updates published to its session channel.
- The dispatcher popped it, handed it to the per-model `Processor`, and the
  Processor asked the Ray controller for a replica — deploying gpt2 if one
  wasn't already HOT.
- The model actor ran the traced block against the real weights and uploaded the
  saved values to MinIO, publishing a presigned download URL on the `COMPLETED`
  response. That URL is signed with `NDIF_OBJECT_STORE_PUBLIC_URL`
  (`http://localhost:9000` in compose) so your *client*, not the server, can
  fetch it.
- The client downloaded the blob, deserialized it, and `hidden` appeared in your
  local frame.

## Before you expose this to anyone

**A bare `just up` has no authentication, and no authentication means every
request runs trusted.** The `NDIF_POSTGRES_URL` line is commented out in the
compose file (`docker-compose.yml:156`), so `PostgresProvider.enabled()` is
False, `verify_api_key` returns `None`, and `validate_request` defaults
`request.trusted` to `True` for any caller that doesn't set it (`api/auth.py:180`).

`trusted` is not a soft label. It decides two things:

- The traced block runs **in-process inside the model actor**, next to the
  weights, instead of in a separate runner process
  (`src/ndif/services/ray/sandbox/model.py`). Whatever Python a caller submits
  executes with the model actor's privileges.
- The flag rides into `trust_remote_code=` when the model loads
  (`cluster/cluster.py:169`), so a request for an arbitrary Hugging Face repo can
  execute that repo's code on your GPU node.

That is a reasonable default for a laptop and a very bad one on a network. On top
of that, the dev compose publishes Redis, Postgres, the Ray client port and the
Ray dashboard to the host with no credentials, and the admin dashboard runs with
`NDIF_DASHBOARD_DEV_MODE=true`, which bypasses its login entirely
(`dashboard/backend/auth.py:73`).

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

- `docs/operating/compose-stack.md` — what each of those eleven containers is for.
- `docs/operating/configuration.md` — how the env-only config model layers.
- `docs/operating/models-and-deployment.md` — deploying, pinning, evicting.
- `docs/operating/troubleshooting.md` — symptom → diagnosis → fix.
- `docs/concepts/request-lifecycle.md` — the five bullets above, in detail.
- `docs/runbooks/enable-auth.md` — turning on API-key auth.
