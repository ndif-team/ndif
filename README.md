<p align="center">
  <img src="./NDIF_Acr_color.png" alt="NDIF" width="300">
</p>

<h3 align="center">
National Deep Inference Fabric
</h3>

<p align="center">
<a href="https://ndif.us"><b>Website</b></a> | <a href="https://nnsight.net"><b>nnsight</b></a> | <a href="https://discord.gg/6uFJmCSwW7"><b>Discord</b></a> | <a href="https://arxiv.org/abs/2407.14561"><b>Paper</b></a>
</p>

---

**NDIF** is the server backend for [**nnsight**](https://nnsight.net). It runs
user-submitted intervention code — hooks, captures, edits, generation — against
large models on a shared GPU cluster. Researchers point nnsight at an NDIF
endpoint and run experiments on models too big to fit on their own hardware.

With no authentication configured — the default — every request is *trusted*
and its code runs inside the model process. Only when auth is on and a key is
not granted `trusted` does the code run in a separate process, one fresh
process per request; that isolation is process-based and still being hardened —
see [docs/concepts/sandbox-execution.md](docs/concepts/sandbox-execution.md).

This repo is the server. For the client, see
[nnsight](https://github.com/ndif-team/nnsight); it is an ordinary dependency
here, and `just up` bind-mounts a local checkout over it for client-side
development.

---

## Installation

The server is published two ways; both carry the same code.

```bash
docker pull ndif/ndif                 # the whole stack in one image (needs the NVIDIA container toolkit)
pip install "ndif[api,ray]"           # the package and the ndif CLI, for running it as host processes
```

A bare `pip install ndif` is only the package and the `ndif` CLI — enough for
`ndif doctor`, `version`, `queue` and `kill`, not for running a server or
talking to one (`deploy`, `status`, `evict` need the Ray client). The `api` and
`ray` extras add the two services and that client. torch is not a dependency of the
package because the right wheel depends on your CUDA driver — install it first
from the matching PyTorch index (`cu126` for any 12.x driver). Both routes, with
the checks that prove they work, are below.

## Quick start

Three ways to stand up your own NDIF. All three need an NVIDIA GPU and a CUDA
driver; the container routes also need the NVIDIA container toolkit.

**Trust default:** with no `NDIF_POSTGRES_URL` the API is unauthenticated, and an
unauthenticated NDIF runs every request **trusted** — the submitted block executes
in-process next to the model weights and models load with `trust_remote_code`. That
is the intended default for running one for yourself. Before anyone else can reach
it, work through [docs/runbooks/enable-auth.md](docs/runbooks/enable-auth.md).

### 1. `docker run` — the published image, whole stack in one container

```bash
docker run --gpus all --shm-size 4g -p 8001:8001 -p 9000:9000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  ndif/ndif:0.1.0
```

`NDIF_SERVICE` defaults to `all`, so the container starts redis, minio, ray and
the API together. 8001 is the API; 9000 is the object store, which the client
downloads results from, so publish it too. Tags: `0.1.0-cu126`, `0.1.0-cu130`,
`0.1.0` (= cu126) and `latest`; pick the CUDA line your driver supports. The
entrypoint is the `ndif` CLI, so any other command works the same way:

```bash
docker run --rm ndif/ndif:0.1.0 version     # ndif, nnsight, torch+CUDA, transformers, ray
docker run --rm --gpus all ndif/ndif:0.1.0 doctor
```

### 2. Docker Compose — the development stack, built from this checkout

Each service in its own container, next to Postgres and the full telemetry set
(Loki, InfluxDB, Prometheus, Grafana). [`just`](https://github.com/casey/just)
wraps the compose commands.

```bash
just up            # build (first time) + start the whole stack, detached
just logs api      # follow a service's logs
just ta            # down -> rebuild -> up, after a source change
just down          # tear it down
```

### 3. From source — the `ndif` CLI, no Docker

No checkout needed — the package is on PyPI:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cu126   # first, or pip picks PyPI's default (CUDA 13) wheel
pip install "ndif[api,ray]"                                              # add metrics,postgres,dashboard as you need them
conda install --override-channels -c conda-forge redis-server minio-server
ndif doctor        # versions, binaries, GPU, connectivity
ndif start         # redis, minio, ray, api — detached
ndif stop          # ...and back down, Ray daemons included
```

From a checkout, `pip install -r requirements.txt` first gives you the pinned
dependency set the image is built from, then `pip install ".[api,ray]"`.
`redis-server` and `minio` come from conda-forge because MinIO no longer
publishes standalone binaries (`--override-channels` sidesteps the anaconda
terms-of-service prompt a stock miniconda raises); the other option is copying
the binary out of the `quay.io/minio/minio` image — see
[docs/operating/quickstart.md](docs/operating/quickstart.md).

### Then run a remote trace

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:8001"

from nnsight.modeling.transformers import TransformersModel
model = TransformersModel("openai-community/gpt2", task="text-generation")

with model.trace("The Eiffel Tower is in the city of", remote=True):
    hidden = model.transformer.h[-1].output.save()
```

No API key is needed; the first request for a model deploys it. Deeper detail for
all three routes is in [docs/operating/quickstart.md](docs/operating/quickstart.md).

## What's running

`just ps` lists the compose stack. The NDIF pieces:

| Service | Where | What |
|---|---|---|
| `api` | `localhost:8001` | Accepts nnsight requests, queues them, streams results back. |
| `ray` | GPU node | Loads models and runs the traced blocks (plain detached Ray actors — NDIF does not use Ray Serve). |
| `dashboard` | `localhost:8081` | Deploy/evict/status, schedules, request monitor. Compose only; not part of `NDIF_SERVICE=all`. |

Redis, MinIO (object store), Postgres (API-key auth), and Loki/InfluxDB/Grafana
(telemetry) round out the compose file; see `docker/docker-compose.yml`.

## Development

```bash
just ta              # down -> rebuild -> up (full refresh after a code change)
just ta ray          # ...targeting a single service
just build && just up
```

The `ndif` CLI is the image entrypoint (`ENTRYPOINT ["ndif"]`, `CMD ["start",
"--foreground"]`); `NDIF_SERVICE` selects which service(s) a container runs. It
accepts a space/comma list, and `all` expands in place — `"all dashboard"` is the
core stack plus the admin UI. Configuration is read from the environment (see the
`environment:` blocks in the compose file).

Agent-facing documentation lives in [CLAUDE.md](CLAUDE.md) and `docs/`.

## Configuration

Everything is configured through `NDIF_*` environment variables. There is no
central config file — each service/provider reads its own vars (with a working
single-host default) at startup, so a bare `just up` runs end-to-end with none of
these set. Override them in the compose `environment:` blocks, a `.env` file, or
the shell. Empty defaults for the optional providers (Postgres/Loki/Influx) mean
that provider is *off* until you set its URL.

**Core / service**

| Variable | Default | Description |
|---|---|---|
| `NDIF_SERVICE` | `all` (image `ENV`) | Which service(s) this container runs: `redis`, `minio`, `ray`, `api`, `dashboard`, `all`, or a space/comma list. `all` means redis, minio, ray, api. |
| `NDIF_ENVIRONMENT` | `dev` | Deployment tag attached to logs/metrics. |
| `NDIF_LOG_LEVEL` | `INFO` | Root log level. |
| `NDIF_HOME` | `~/.ndif` | CLI state directory. |

**API**

| Variable | Default | Description |
|---|---|---|
| `NDIF_API_URL` | `http://localhost:8001` | Base URL of the API (compose uses `http://api:8001`). |
| `NDIF_API_PORT` | `8001` | Port the API binds. |
| `NDIF_API_WORKERS` | `1` | Gunicorn worker count. |
| `NDIF_API_TIMEOUT` | `120` | Gunicorn worker timeout (seconds). |
| `NDIF_API_KEY` | _(unset)_ | API key the dashboard's monitor cron sends with its probe traces (`jobs/monitor.py`). Not read by the CLI or the API. |

**Request queue**

| Variable | Default | Description |
|---|---|---|
| `NDIF_QUEUE_KEY` | `queue` | Redis key backing the request queue. |
| `NDIF_QUEUE_FETCH_TIMEOUT_S` | `10` | Blocking-pop timeout when draining the queue. |
| `NDIF_QUEUE_FETCH_BATCH_MAX` | `32` | Max requests pulled per fetch. |

**Autoscaling**

| Variable | Default | Description |
|---|---|---|
| `NDIF_AUTOSCALING_INTERVAL_S` | `5` | How often the scaler evaluates the queue. |
| `NDIF_AUTOSCALING_BACKOFF_S` | `120` | Pause after a scale-up so the new replica can warm. |
| `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` | `30` | Queue wait time that triggers a scale-up. |
| `NDIF_AUTOSCALING_MAX_REPLICAS` | `3` | Replica ceiling per model. |

**Ray / cluster**

| Variable | Default | Description |
|---|---|---|
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | Ray client address the API/dashboard connect to. |
| `NDIF_RAY_HEAD_ADDRESS` | _(empty)_ | Head-node address workers join (empty = start a head). |
| `NDIF_RAY_HEAD_PORT` | `6385` | Ray GCS head port (offset from Redis's 6379). |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | Ray dashboard port. |
| `NDIF_RAY_DASHBOARD_GRPC_PORT` | `52366` | Ray dashboard gRPC port. |
| `NDIF_RAY_METRICS_PORT` | `8080` | Ray's `--metrics-export-port` (the Prometheus scrape target). Not a Ray Serve port. |
| `NDIF_RAY_OBJECT_MANAGER_PORT` | `8076` | Ray object-manager port. |
| `NDIF_RAY_RESOURCE_NAME` | _(empty)_ | Custom Ray resource label for this node. |
| `NDIF_RAY_TEMP_DIR` | `/tmp/ray` | Ray temp/session directory. |
| `NDIF_RAY_HEAD_WAIT_INTERVAL_S` | `2` | Worker poll interval while waiting for the head. |
| `NDIF_RAY_HEAD_WAIT_RETRIES` | `60` | Worker retries before giving up on the head. |

**Controller / deployments**

| Variable | Default | Description |
|---|---|---|
| `NDIF_DEPLOYMENTS` | _(empty)_ | `|`-separated model keys to deploy on boot. |
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | How often the controller re-syncs its node set. Deployment changes are event-driven, not polled. |
| `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` | Minimum lifetime before a model can be evicted. |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | Fraction of the node's **host RAM** the WARM (off-GPU) model cache may use. Not a GPU knob. |
| `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | `ndif.services.ray.deployments.modeling.base.ModelActor` | Actor class used to serve a model. Compose sets the sandboxed `...ray.sandbox.model.SandboxModelActor`. |
| `NDIF_TP_MODEL_ACTOR_CLASS` | _(unset)_ | Tensor-parallel actor class. Unset means tensor parallelism is off entirely. |
| `NDIF_DEFAULT_DTYPE` | `bfloat16` | Dtype models load in. |
| `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | _(unset)_ | Per-request execution cap. Unset means no cap — set it before others can submit. |
| `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | Batch-padding memory factor. |
| `NDIF_DEFAULT_PADDING_BIAS` | `524288000` | Batch-padding memory bias in bytes (500 MiB). |
| `NDIF_MIN_NNSIGHT_VERSION` | _(unset)_ | Minimum client nnsight version accepted. |
| `NDIF_MIN_PYTHON_VERSION` | _(unset)_ | Minimum client Python version accepted. |

**Redis / caches**

| Variable | Default | Description |
|---|---|---|
| `NDIF_REDIS_URL` | `redis://localhost:6379` | Redis connection URL. |
| `NDIF_ENV_TTL_S` | `300` | TTL of the cached model-environment metadata. |
| `NDIF_ENV_TIMEOUT_S` | `60` | Timeout awaiting a fresh env entry. |
| `NDIF_STATUS_TTL_S` | `60` | TTL of the cached deployment status. |
| `NDIF_STATUS_TIMEOUT_S` | `60` | Timeout awaiting a fresh status entry. |
| `NDIF_STATUS_CACHE_FREQ_S` | `10` | Refresh frequency of the API's Redis-backed `/status` cache. |

**Object store (S3 / MinIO)**

| Variable | Default | Description |
|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | S3-compatible endpoint result blobs stage to. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | _(empty)_ | Public URL used when presigning (defaults to the endpoint). |
| `NDIF_OBJECT_STORE_ACCESS_KEY` | `minioadmin` | Access key. |
| `NDIF_OBJECT_STORE_SECRET_KEY` | `minioadmin` | Secret key. |
| `NDIF_OBJECT_STORE_BUCKET` | `ndif-results` | Bucket for result blobs. |
| `NDIF_OBJECT_STORE_REGION` | `us-east-1` | Region sent to the S3 client. |
| `NDIF_OBJECT_STORE_VERIFY` | `true` | Verify TLS to the endpoint. |
| `NDIF_OBJECT_STORE_CONSOLE_PORT` | `9001` | MinIO web console port (compose only). |

**Auth — Postgres** _(empty URL ⇒ API runs unauthenticated)_

| Variable | Default | Description |
|---|---|---|
| `NDIF_POSTGRES_URL` | _(empty)_ | Connection URL for the user/API-key DB; empty disables auth. |
| `NDIF_POSTGRES_POOL_MIN` | `1` | Connection-pool minimum size. |
| `NDIF_POSTGRES_POOL_MAX` | `10` | Connection-pool maximum size. |
| `NDIF_POSTGRES_COMMAND_TIMEOUT_S` | `10.0` | Per-command timeout (seconds). |

**Telemetry — InfluxDB (metrics)**

| Variable | Default | Description |
|---|---|---|
| `NDIF_INFLUX_URL` | _(unset — metrics off)_ | InfluxDB endpoint; set it to turn metrics on. |
| `NDIF_INFLUX_TOKEN` | _(empty)_ | Write token. |
| `NDIF_INFLUX_ORG` | `ndif` | Influx organization. |
| `NDIF_INFLUX_BUCKET` | `metrics` | Target bucket. |
| `NDIF_INFLUX_ENABLED` | `true` | Master switch for metric writes. |
| `NDIF_INFLUX_BATCH_SIZE` | `500` | Points buffered before a flush. |
| `NDIF_INFLUX_FLUSH_INTERVAL_MS` | `1000` | Max time between flushes (ms). |
| `NDIF_INFLUX_TIMEOUT_MS` | `10000` | Write request timeout (ms). |

**Telemetry — Loki (logs)** _(empty URL ⇒ console-only logging)_

| Variable | Default | Description |
|---|---|---|
| `NDIF_LOKI_URL` | _(empty)_ | Loki push endpoint; empty disables log shipping. |
| `NDIF_LOKI_LEVEL` | `INFO` | Minimum level shipped to Loki. |
| `NDIF_LOKI_QUEUE_MAX` | `10000` | Max buffered log records before dropping. |

**Dashboard**

| Variable | Default | Description |
|---|---|---|
| `NDIF_DASHBOARD_PORT` | `8081` | Port the dashboard binds. |
| `NDIF_DASHBOARD_USERNAME` | `admin` | Admin username. |
| `NDIF_DASHBOARD_PASSWORD_HASH` | _(empty)_ | Bcrypt hash of the admin password. |
| `NDIF_DASHBOARD_SESSION_SECRET` | `change-me-please-this-is-not-secure` | Cookie-signing secret — **set this in prod**. |
| `NDIF_DASHBOARD_SESSION_TTL_DAYS` | `7` | Session cookie lifetime (days). |
| `NDIF_DASHBOARD_DEV_MODE` | `false` | Bypasses the dashboard login entirely. Compose sets it `true`. |
| `NDIF_DASHBOARD_API_URL` | `http://localhost:8001` | NDIF API URL (falls back to `NDIF_API_URL`). |
| `NDIF_DASHBOARD_DATA_DIR` | `~/ndif_dashboard` | Dashboard state directory. |
| `NDIF_DASHBOARD_FRONTEND_DIST` | `<package>/frontend/dist` | Built Vue UI directory to serve. |
| `NDIF_DASHBOARD_MONITOR_URL` | `http://localhost:8001` | Target the monitor cron probes. |
| `NDIF_DASHBOARD_MONITOR_CRON` | `*/10 * * * *` | Monitor cron schedule. |
| `NDIF_DASHBOARD_RECONCILE_CRON` | `*/2 * * * *` | Reconcile cron schedule. |

## Using NDIF from an LLM agent

Give an agent up-to-date knowledge of running and operating NDIF one of these
ways:

- **Skills** — in Claude Code: `/plugin marketplace add https://github.com/ndif-team/skills.git`
  then `/plugin install ndif@ndif-team` (the `nnsight` plugin from the same
  marketplace covers the client side). In OpenAI Codex:
  `skill-installer install https://github.com/ndif-team/skills.git`.
- **Context7 MCP** — add `use context7` to prompts, or point your MCP client at
  `https://mcp.context7.com/mcp` (see [Context7](https://github.com/upstash/context7)).
- **Docs in context** — hand the agent [CLAUDE.md](./CLAUDE.md), which routes by
  task into [docs/](./docs/); every page there cites the source it describes.

## Contributing

PRs welcome. Please read the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENCE) © Northeastern University.

## Citation

```bibtex
@article{fiottokaufman2024nnsightndifdemocratizingaccess,
      title={NNsight and NDIF: Democratizing Access to Foundation Model Internals},
      author={Jaden Fiotto-Kaufman and Alexander R Loftus and Eric Todd and Jannik Brinkmann and Caden Juang and Koyena Pal and Can Rager and Aaron Mueller and Samuel Marks and Arnab Sen Sharma and Francesca Lucchetti and Michael Ripa and Adam Belfki and Nikhil Prakash and Sumeet Multani and Carla Brodley and Arjun Guha and Jonathan Bell and Byron Wallace and David Bau},
      year={2024},
      eprint={2407.14561},
      archivePrefix={arXiv},
      primaryClass={cs.LG},
      url={https://arxiv.org/abs/2407.14561},
}
```
