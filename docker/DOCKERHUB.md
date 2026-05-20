# NDIF — National Deep Inference Fabric

The server backend for [**nnsight**](https://nnsight.net) — run remote interventions, hook captures, and edits on large language models in a sandboxed Ray cluster.

This image bundles the **entire NDIF stack** (API + Ray + Redis + MinIO + optional dashboard) in a single container, so you can stand up a working backend with one `docker run` and point nnsight at it.

```bash
docker run --rm --gpus all \
    -p 5001:5001 -p 27018:27018 -p 8265:8265 \
    -v $HOME/.cache/huggingface:/root/.cache/huggingface \
    ndif/ndif:latest
```

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:5001"
nnsight.CONFIG.set_default_api_key("any-key-works-in-dev-mode")

model = nnsight.LanguageModel("openai-community/gpt2")
with model.trace("Hello world", remote=True):
    h6 = model.transformer.h[6].output[0].save()
print(h6.shape)   # → torch.Size([3, 768])
```

---

## What's inside

| Component | Role |
|---|---|
| **API** (FastAPI + Gunicorn) | HTTP entry, request validation, Socket.IO status stream |
| **Ray** (head + ModelActor) | Cluster + GPU-scheduled model serving |
| **Redis** (broker) | Job queue, pub/sub, Socket.IO backend |
| **MinIO** (object store) | S3-compatible bucket for trace results |
| **Dashboard** (optional) | Admin web UI + monitor/reconcile cron jobs |

All five services run in one container, supervised by `ndif start --verbose`. Dev mode is **on by default** (`NDIF_DEV_MODE=true`) — any API key works, no Postgres needed.

**Requires an NVIDIA GPU** (`--gpus all`). Bundles a CUDA-enabled PyTorch build (whatever version the bundled `torch` resolves to at build time — runs on any reasonably modern NVIDIA driver).

---

## Tags

| Tag | Description |
|---|---|
| `latest` | Most recent build, tracks the `main` branch |
| `0.0.1` | Pinned to a specific NDIF version — recommended for production |

---

## Ports

| Port | Service | When to publish |
|---|---|---|
| **5001** | API | Always — this is what nnsight connects to |
| **27018** | MinIO S3 | Always — clients pull trace results from here |
| **8081** | Dashboard | Only when you enable the dashboard (see below) |
| **8265** | Ray dashboard | Optional, debugging only |
| `6379` | Redis broker | Internal to the container; rarely useful to publish |
| `6385` | Ray head | Internal |
| `10001` | Ray client | Only if you want to attach a worker node from outside |
| `8262`, `8076`, `8268`, `46805` | Ray serve / object-manager / dashboard-gRPC / MinIO console | Internal |

---

## Volumes

| Mount | Why |
|---|---|
| `~/.cache/huggingface → /root/.cache/huggingface` | **Recommended** — share your HF cache so the container doesn't re-download models on every run, and so gated tokens (Llama, Gemma) work |
| `<your-data> → /root/.ndif/data` | Persist Redis + MinIO state across container restarts |
| `<your-models> → ~/.ndif/models.yaml` | A model-config file the CLI auto-deploys at startup (see [`models.yaml` docs](https://github.com/ndif-team/ndif)) |

---

## Enabling the dashboard

The dashboard is **off by default**. Turn it on by setting `NDIF_DASHBOARD_PORT`:

```bash
docker run --rm --gpus all \
    -p 5001:5001 -p 27018:27018 -p 8081:8081 \
    -v $HOME/.cache/huggingface:/root/.cache/huggingface \
    -e NDIF_DASHBOARD_PORT=8081 \
    ndif/ndif:latest
```

By default the dashboard has **no login** (`NDIF_DASHBOARD_PASSWORD_HASH` empty). To require login, set a bcrypt hash:

```bash
# generate a hash for your password
docker run --rm ndif/ndif:latest \
    python -m ndif.services.dashboard.backend.auth hash "your-password"

# then pass the hash + a random session secret
docker run ... \
    -e NDIF_DASHBOARD_PORT=8081 \
    -e NDIF_DASHBOARD_USERNAME=admin \
    -e NDIF_DASHBOARD_PASSWORD_HASH='$2b$12$...' \
    -e NDIF_DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)" \
    ndif/ndif:latest
```

---

## Environment variables

Defaults applied at startup; override any with `-e VAR=value`.

### General

| Variable | Default | Description |
|---|---|---|
| `NDIF_DEV_MODE` | `true` | If `true`, skips API-key validation (any key works). Set `false` only if you also wire up Postgres for the keys DB. |
| `NDIF_API_KEY` | — | Default API key used by internal cron jobs (dashboard monitor). Not consulted in dev mode. |

### API

| Variable | Default | Description |
|---|---|---|
| `NDIF_API_URL` | `http://localhost:5001` | Public URL the API advertises (also used internally by Ray for callbacks) |
| `NDIF_API_PORT` | `5001` | Port the API listens on |
| `NDIF_API_WORKERS` | `1` | Number of Gunicorn workers (raise on a beefier host) |

### Broker (Redis)

| Variable | Default | Description |
|---|---|---|
| `NDIF_BROKER_URL` | `redis://localhost:6379` | Broker URL the API + Ray use |
| `NDIF_BROKER_PORT` | `6379` | Listen port (internal Redis daemon) |

### Object store (MinIO)

| Variable | Default | Description |
|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://localhost:27018` | S3 endpoint that clients hit to pull results |
| `NDIF_OBJECT_STORE_PORT` | `27018` | MinIO S3 API port |
| `NDIF_OBJECT_STORE_CONSOLE_PORT` | `46805` | MinIO admin console port |
| `NDIF_OBJECT_STORE_SERVICE` | `s3` | Service type (`s3` — internal switch) |
| `NDIF_OBJECT_STORE_BUCKET` | `ndif-results` | Bucket name used for results |
| `NDIF_OBJECT_STORE_ACCESS_KEY` | `minioadmin` | S3 access key |
| `NDIF_OBJECT_STORE_SECRET_KEY` | `minioadmin` | S3 secret key |
| `NDIF_OBJECT_STORE_REGION` | `us-east-1` | Region (cosmetic for MinIO) |
| `NDIF_OBJECT_STORE_VERIFY` | `false` | Verify TLS certs (`false` for in-container HTTP) |

### Ray

| Variable | Default | Description |
|---|---|---|
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | Ray client address the API uses |
| `NDIF_RAY_HEAD_PORT` | `6385` | Ray head GCS port |
| `NDIF_RAY_CLIENT_PORT` | `10001` | Ray client port |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | Ray's own dashboard (different from NDIF's) |
| `NDIF_RAY_SERVE_PORT` | `8262` | Ray Serve / metrics port |
| `NDIF_RAY_OBJECT_MANAGER_PORT` | `8076` | Object manager port |
| `NDIF_RAY_DASHBOARD_GRPC_PORT` | `8268` | Dashboard gRPC port |
| `NDIF_RAY_TEMP_DIR` | `/tmp/ray` | Ray spill / temp dir (volume-mount if you want to persist logs) |

### Controller / scheduler

| Variable | Default | Description |
|---|---|---|
| `NDIF_CONTROLLER_IMPORT_PATH` | `ndif.services.ray.deployments.controller.controller` | Dotted path to the Controller actor class. Override to plug in a custom scheduler. |
| `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | `ndif.services.ray.deployments.modeling.base.ModelActor` | Dotted path to the default ModelActor class. |
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | How often the Controller re-reconciles deployment state. |
| `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `0` | Min lifetime before a hot-swapped model can be evicted. Raise to avoid thrash. |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | Fraction of GPU memory to reserve for model weights. |
| `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | `3600` | Per-request execution timeout. |
| `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | Activation-memory padding factor (fraction of param size). |
| `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MiB) | Activation-memory padding bias (bytes). |
| `NDIF_DEPLOYMENTS` | — | Pipe-delimited list of model keys to deploy at startup (alternative to `models.yaml`). |

### Dashboard

The dashboard is included in the image but **only starts when `NDIF_DASHBOARD_PORT` is set**.

| Variable | Default | Description |
|---|---|---|
| `NDIF_DASHBOARD_PORT` | — (unset = disabled) | Set this to enable the dashboard. Conventional value: `8081`. |
| `NDIF_DASHBOARD_USERNAME` | `admin` | Admin username |
| `NDIF_DASHBOARD_PASSWORD_HASH` | empty (no login) | Bcrypt hash of the admin password. Empty = login disabled. |
| `NDIF_DASHBOARD_SESSION_SECRET` | (placeholder) | 32+ byte random string for signing cookies. **Set this in production.** |
| `NDIF_DASHBOARD_DATA_DIR` | `~/ndif_dashboard` | Where logs / `schedule.json` / state live |
| `NDIF_DASHBOARD_MONITOR_URL` | `$NDIF_API_URL` | URL the monitor cron probes (usually leave default) |
| `NDIF_DASHBOARD_FRONTEND_DIST` | bundled | Override if you want to serve a custom-built frontend dist |
| `NDIF_DASHBOARD_MONITOR_CRON` | `*/10 * * * *` | Cron schedule for the uptime monitor job |
| `NDIF_DASHBOARD_RECONCILE_CRON` | `*/2 * * * *` | Cron schedule for the deployment-reconcile job |
| `NDIF_DASHBOARD_SESSION_TTL_DAYS` | `7` | Login cookie TTL |

### HuggingFace

| Variable | Default | Description |
|---|---|---|
| `HF_TOKEN` | — | HuggingFace token. Required for gated models (Llama, Gemma). |
| `HF_HOME` | `/root/.cache/huggingface` | HF cache dir. Volume-mount this from your host. |

---

## Examples

**Quick GPT-2 demo:**

```bash
docker run --rm --gpus all -p 5001:5001 -p 27018:27018 \
    -v $HOME/.cache/huggingface:/root/.cache/huggingface \
    ndif/ndif:latest
```

**With the dashboard and a real password:**

```bash
docker run --rm --gpus all \
    -p 5001:5001 -p 27018:27018 -p 8081:8081 \
    -v $HOME/.cache/huggingface:/root/.cache/huggingface \
    -e NDIF_DASHBOARD_PORT=8081 \
    -e NDIF_DASHBOARD_PASSWORD_HASH='$2b$12$...' \
    -e NDIF_DASHBOARD_SESSION_SECRET="$(openssl rand -hex 32)" \
    ndif/ndif:latest
```

**With a gated model (Llama):**

```bash
docker run --rm --gpus all -p 5001:5001 -p 27018:27018 \
    -v $HOME/.cache/huggingface:/root/.cache/huggingface \
    -e HF_TOKEN="hf_..." \
    -e NDIF_DEPLOYMENTS="meta-llama/Llama-3.1-8B-Instruct" \
    ndif/ndif:latest
```

**Persisting Redis + MinIO state across restarts:**

```bash
docker run --rm --gpus all -p 5001:5001 -p 27018:27018 \
    -v $HOME/.cache/huggingface:/root/.cache/huggingface \
    -v ndif-data:/root/.ndif/data \
    ndif/ndif:latest
```

---

## Links

- **nnsight client**: <https://nnsight.net>
- **Source code**: <https://github.com/ndif-team/ndif>
- **Issues**: <https://github.com/ndif-team/ndif/issues>
- **NDIF project home**: <https://ndif.us>

---

## License

See [LICENSE](https://github.com/ndif-team/ndif/blob/main/LICENSE) in the repo.
