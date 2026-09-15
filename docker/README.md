# NDIF — National Deep Inference Fabric

The server behind [**nnsight**](https://nnsight.net). It runs nnsight
intervention code — activation captures, edits, patching, generation — against
models on your GPUs, so anything you write with `remote=True` runs on your own
machine instead of the public NDIF service.

This image contains the **whole stack**: the HTTP API, a Ray node that loads
models and runs traced code, and the Redis and MinIO instances they use. One
`docker run` gives you a working backend.

- Source and docs: <https://github.com/ndif-team/ndif>
- Client library: <https://github.com/ndif-team/nnsight> · [nnsight.net](https://nnsight.net)
- Discord: <https://discord.gg/6uFJmCSwW7>

## Quick start

You need an NVIDIA GPU, a driver that supports CUDA 12.6 or newer, and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
(`docker run --gpus all` has to work).

```bash
docker run -d --name ndif --gpus all --shm-size 4g \
    -p 8001:8001 -p 9000:9000 \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    -e HF_TOKEN \
    ndif/ndif
```

Ray takes a minute to boot. `curl localhost:8001/ping` answers `"pong"` as
soon as the API is up; `curl localhost:8001/connected` answers
`{"status":"connected"}` once the API can reach Ray, and that is when the
server is ready. Until then the API answers 503 and logs
`Error connecting to Ray` once a second — that is expected during boot.

Then, on the client side, with `pip install nnsight` (0.8 or newer):

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:8001"

from nnsight.modeling.transformers import TransformersModel
model = TransformersModel("openai-community/gpt2", task="text-generation")

with model.trace("The Eiffel Tower is in the city of", remote=True):
    hidden = model.transformer.h[-1].output.save()

print(hidden.shape)   # torch.Size([1, 10, 768])
```

Nothing is deployed ahead of time: the first request for a checkpoint downloads
its weights and loads it, later requests reuse it. No API key is needed — the
image runs with authentication off.

Stop it with `docker stop ndif`; the downloaded weights stay in your Hugging
Face cache.

## Tags

| Tag | Torch wheel | Use when |
|---|---|---|
| `latest`, `0.1.0`, `0.1.0-cu126` | CUDA 12.6 | Default. Runs on any CUDA 12.x driver (>= 525). |
| `0.1.0-cu130` | CUDA 13.0 | Drivers at 580 or newer, and Blackwell GPUs (RTX 50xx, B200), which the cu126 wheel has no kernels for. |

Pick by the CUDA version in `nvidia-smi`'s header: a 12.x driver runs cu126;
a 13.x driver runs either. A wheel newer than your driver supports fails at
runtime with `the NVIDIA driver on your system is too old`. There is no cu128
tag: PyTorch's cu128 index stopped at torch 2.11.

Ask an image what it carries:

```bash
docker run --rm ndif/ndif version
```

The versions that decide server behaviour, for `0.1.0`:

| | cu126 | cu130 |
|---|---|---|
| ndif | 0.1.0 | 0.1.0 |
| nnsight | 0.8.0rc1 | 0.8.0rc1 |
| torch | 2.14.0+cu126 | 2.14.0+cu130 |
| transformers | 5.17.0 | 5.17.0 |
| ray | 2.55.1 | 2.55.1 |
| Python | 3.12 | 3.12 |

The client and server should run the same nnsight minor version and the same
transformers major version: a traced block refers to modules by their position
in the model tree, and the tree has to match on both sides. `ndif` and its
docs live at <https://github.com/ndif-team/ndif>; nnsight releases at
<https://pypi.org/project/nnsight/>.

## What is in the container

`NDIF_SERVICE` selects what the container runs. The default, `all`, is the core
stack:

| Service | What it does | Port |
|---|---|---|
| `api` | Accepts nnsight requests, queues them, streams status and results back | 8001 |
| `ray` | Ray head node; loads models on the GPU and runs the traced blocks | 8265 (Ray dashboard), 10001 (Ray client) |
| `redis` | Request queue and response pub/sub | 6379 |
| `minio` | S3-compatible store for results too large to ride on the response | 9000 (S3), 9001 (console) |
| `dashboard` | Admin web UI: deploy / evict / status / schedules. Opt-in, see below | 8081 |

Publish only what you use. `8001` is the one port a client needs; `9000` is
needed as well for results over 20 MB (they come back as a presigned MinIO
URL the client downloads); `8265` if you want the Ray dashboard.

## Configuration

Everything is an environment variable; the image needs none set to work. The
ones you are likely to want:

| Variable | Default | Purpose |
|---|---|---|
| `NDIF_SERVICE` | `all` | Which services to run: `all`, or a space/comma list of `redis minio ray api dashboard`. `all dashboard` adds the admin UI. |
| `HF_TOKEN` | unset | Hugging Face token for gated checkpoints (Llama, Gemma, ...). |
| `HF_HOME` | `/root/.cache/huggingface` | Model weight cache. Mount your host cache there so weights persist. |
| `NDIF_DEPLOYMENTS` | unset | Model *keys* (the long `TransformersModel:{"repo_id": ...}` strings `ndif status --verbose` prints) to load pinned at start, `\|`-separated. Repo ids are not accepted here; to pre-load a checkpoint by name run `docker exec ndif ndif deploy <repo-id>` once the container is up. Otherwise models load on first request. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | `http://localhost:9000` | The MinIO address **as the client sees it**. Set it to `http://<this-host>:9000` when clients run on other machines, or large results fail to download. |
| `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | Head-room added to a model's memory estimate when placing it. It also caps how much GPU memory a block may allocate beyond the weights; raise it if your interventions die with `CUDA out of memory ... MiB allowed`. |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | Share of host RAM evicted models may occupy before being dropped. |
| `NDIF_API_PORT` | `8001` | API port inside the container. |
| `NDIF_RAY_TEMP_DIR` | `/tmp/ray` | Ray's session directory. Ray refuses to schedule work when the filesystem it is on is more than 95 % full. |

GPUs: `--gpus all` hands over every GPU; `--gpus '"device=0,1"'` restricts to
some. NDIF sizes placement from each GPU's total memory, so on a shared machine
give it cards nobody else is using.

Shared memory: Ray's object store lives in `/dev/shm`. Docker's default 64 MB
is far too small; `--shm-size 4g` is a floor, go higher for large models.

The full variable list, with the file that reads each one, is
[docs/reference/env-vars.md](https://github.com/ndif-team/ndif/blob/main/docs/reference/env-vars.md).

## Running the admin dashboard

```bash
docker run -d --name ndif --gpus all --shm-size 4g \
    -p 8001:8001 -p 9000:9000 -p 8081:8081 \
    -v ~/.cache/huggingface:/root/.cache/huggingface \
    -e NDIF_SERVICE="all dashboard" \
    -e NDIF_DASHBOARD_DEV_MODE=true \
    ndif/ndif
```

`NDIF_DASHBOARD_DEV_MODE=true` disables its login. For a dashboard other
people can reach, drop that and set `NDIF_DASHBOARD_USERNAME`, a bcrypt
`NDIF_DASHBOARD_PASSWORD_HASH` and a random `NDIF_DASHBOARD_SESSION_SECRET`
(see [docs/operating/dashboard.md](https://github.com/ndif-team/ndif/blob/main/docs/operating/dashboard.md)).

## Managing models

The `ndif` CLI is the image's entrypoint, so every command runs through
`docker exec` on the running container:

```bash
docker exec ndif ndif status                              # what is loaded, GPU headroom
docker exec ndif ndif deploy meta-llama/Llama-3.1-8B      # load a model ahead of time
docker exec ndif ndif evict meta-llama/Llama-3.1-8B       # free its GPU memory
docker exec ndif ndif queue                               # queued and in-flight requests
docker exec ndif ndif doctor                              # versions, binaries, GPU, connectivity
```

Every command with its options:
[docs/operating/cli.md](https://github.com/ndif-team/ndif/blob/main/docs/operating/cli.md).
Sizing, revisions, dtypes and pinning:
[docs/operating/models-and-deployment.md](https://github.com/ndif-team/ndif/blob/main/docs/operating/models-and-deployment.md).

## Who should reach this

**This image is for running NDIF for yourself.** With authentication off (the
default — there is no Postgres user database in the container) every request
is treated as *trusted*: the submitted code runs inside the model process, and
checkpoints load with `trust_remote_code`. That is the right default on your
own machine and the wrong one on a network. Before you let anyone else send
requests, read
[docs/runbooks/enable-auth.md](https://github.com/ndif-team/ndif/blob/main/docs/runbooks/enable-auth.md)
and
[docs/operating/production.md](https://github.com/ndif-team/ndif/blob/main/docs/operating/production.md).

## Something is wrong

| Symptom | Cause |
|---|---|
| `docker run` exits at once with `could not select device driver "" with capabilities: [[gpu]]` | The NVIDIA Container Toolkit isn't installed or Docker wasn't restarted after installing it. |
| Logs show `cuda_memory_bytes: 0` in `Starting Ray head node with resources` | Torch can't see the GPU. `docker run --rm --gpus all ndif/ndif doctor` says whether the tag's CUDA line is newer than your driver (`torch cannot use the GPU (driver CUDA 12.5, torch built for CUDA 13.0)`): switch tags. |
| `/connected` says `reconnecting` for more than a few minutes | Ray failed to start. `docker logs ndif` — look at the first error after `Starting Ray head node`. |
| Trace hangs in `DEPLOYING` | Weights are downloading. Pre-download into the mounted cache, or watch `docker logs ndif`. |
| `Cannot access gated repo` / 401 from Hugging Face | Pass `-e HF_TOKEN` (and accept the model's licence on the Hub). |
| Result download fails from another machine | Set `NDIF_OBJECT_STORE_PUBLIC_URL` to the address that machine can reach, and publish port 9000. |
| `CUDA out of memory ... MiB allowed` inside a block | The per-model memory cap. Raise `NDIF_DEFAULT_PADDING_FACTOR`, or save less. |
| `The model architecture on this server doesn't match ...` | Client and server transformers (or nnsight) versions differ. `docker run --rm ndif/ndif version` vs `pip show nnsight transformers`. |

More: [docs/operating/troubleshooting.md](https://github.com/ndif-team/ndif/blob/main/docs/operating/troubleshooting.md).

## Other ways to run NDIF

- **Docker Compose from the source tree** — one container per service, plus
  Grafana, Loki, InfluxDB, Prometheus and Postgres, and a bind-mounted nnsight
  checkout for client-side development. `git clone
  https://github.com/ndif-team/ndif && cd ndif && just up`. See
  [docs/operating/quickstart.md](https://github.com/ndif-team/ndif/blob/main/docs/operating/quickstart.md).
- **From source, no Docker** — `pip install ndif[api,ray]` plus `redis-server`
  and `minio` on the PATH, then `ndif start`. Same page.
- **Multiple GPU machines** — run `ray` on each extra node with
  `NDIF_RAY_HEAD_ADDRESS` pointing at the head:
  [docs/runbooks/add-a-gpu-node.md](https://github.com/ndif-team/ndif/blob/main/docs/runbooks/add-a-gpu-node.md).

## Building the image

```bash
git clone https://github.com/ndif-team/ndif && cd ndif
docker build -f docker/Dockerfile -t ndif/ndif:dev .                          # cu126
docker build -f docker/Dockerfile --build-arg TORCH_CUDA=cu130 -t ndif/ndif:dev .
docker build -f docker/Dockerfile --build-arg TORCH_SPEC="torch==2.11.0" -t ndif/ndif:dev .
```

`TORCH_CUDA` picks the PyTorch wheel index; `TORCH_SPEC` pins the torch
version. Everything else is pinned in `requirements.txt`.

## License

MIT. See [LICENCE](https://github.com/ndif-team/ndif/blob/main/LICENCE).
