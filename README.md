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

Untrusted intervention code runs in a separate process from the model, one fresh
process per request. That isolation is process-based and still being hardened —
see [docs/concepts/sandbox-execution.md](docs/concepts/sandbox-execution.md).

This repo is the server. For the client, see
[nnsight](https://github.com/ndif-team/nnsight); it is an ordinary dependency
here, and `just up` bind-mounts a local checkout over it for client-side
development.

---

## Quick start

The stack runs as a set of Docker Compose services (API, a GPU Ray cluster, a
dashboard, and supporting stores). [`just`](https://github.com/casey/just) wraps
the compose commands — the GPU `ray` service needs a host GPU and the NVIDIA
container toolkit.

```bash
just up            # build (first time) + start the whole stack, detached
just logs api      # follow a service's logs
just down          # tear it down
```

Then point nnsight at the local server and run remotely:

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:8001"

from nnsight.modeling.transformers import TransformersModel
model = TransformersModel("openai-community/gpt2", task="text-generation")

with model.trace("The Eiffel Tower is in the city of", remote=True):
    hidden = model.transformer.h[-1].output.save()
```

## What's running

`just ps` lists the stack. The main pieces:

| Service | Where | What |
|---|---|---|
| `api` | `localhost:8001` | Accepts nnsight requests, queues them, streams results back. |
| `ray` | GPU node | Loads models and runs the traced blocks (Ray Serve deployments). |
| `dashboard` | `localhost:8081` | Deploy/evict/status, schedules, request monitor. |

Redis, MinIO (object store), Postgres (API-key auth), and Loki/InfluxDB/Grafana
(telemetry) round out the compose file; see `docker/docker-compose.yml`.

## Development

```bash
just ta              # down -> rebuild -> up (full refresh after a code change)
just ta ray          # ...targeting a single service
just build && just up
```

The `ndif` CLI (`ndif start <service>`, the image entrypoint) runs a service in
the foreground; `NDIF_SERVICE` selects which one per container. Configuration is
read from the environment (see the `environment:` blocks in the compose file).

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
| `NDIF_SERVICE` | `api` | Which service this container runs (`api`, `ray`, `dashboard`). |
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
| `NDIF_API_KEY` | _(unset)_ | Client API key used by the `ndif` CLI. |

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
| `NDIF_RAY_SERVE_PORT` | `8080` | Ray Serve HTTP port. |
| `NDIF_RAY_OBJECT_MANAGER_PORT` | `8076` | Ray object-manager port. |
| `NDIF_RAY_RESOURCE_NAME` | _(empty)_ | Custom Ray resource label for this node. |
| `NDIF_RAY_TEMP_DIR` | `/tmp/ray` | Ray temp/session directory. |
| `NDIF_RAY_HEAD_WAIT_INTERVAL_S` | `2` | Worker poll interval while waiting for the head. |
| `NDIF_RAY_HEAD_WAIT_RETRIES` | `60` | Worker retries before giving up on the head. |

**Controller / deployments**

| Variable | Default | Description |
|---|---|---|
| `NDIF_DEPLOYMENTS` | _(empty)_ | `|`-separated model keys to deploy on boot. |
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | Reconcile cadence for the deployment controller. |
| `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` | Minimum lifetime before a model can be evicted. |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | Fraction of GPU memory reserved for the model cache. |
| `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | `ndif.services.ray.deployments.modeling.base.ModelActor` | Actor class used to serve a model. |
| `NDIF_DEFAULT_DTYPE` | `bfloat16` | Dtype models load in. |
| `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | `3600` | Per-request execution cap. |
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
| `NDIF_INFLUX_URL` | `http://localhost:8086` | InfluxDB endpoint. |
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
| `NDIF_DASHBOARD_DEV_MODE` | `false` | Enable dev conveniences. |
| `NDIF_DASHBOARD_API_URL` | `http://localhost:8001` | NDIF API URL (falls back to `NDIF_API_URL`). |
| `NDIF_DASHBOARD_DATA_DIR` | `~/ndif_dashboard` | Dashboard state directory. |
| `NDIF_DASHBOARD_FRONTEND_DIST` | `<package>/frontend/dist` | Built Vue UI directory to serve. |
| `NDIF_DASHBOARD_MONITOR_URL` | `http://localhost:8001` | Target the monitor cron probes. |
| `NDIF_DASHBOARD_MONITOR_CRON` | `*/10 * * * *` | Monitor cron schedule. |
| `NDIF_DASHBOARD_RECONCILE_CRON` | `*/2 * * * *` | Reconcile cron schedule. |

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
