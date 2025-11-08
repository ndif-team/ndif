# NDIF Ray Service

The **Ray service** is NDIF’s distributed execution layer. It runs and scales NDIF workloads (inference, preprocessing, background jobs), exposes Ray Serve deployments, and integrates with NDIF’s telemetry stack (Prometheus/Grafana, Loki, and InfluxDB).  

This container typically runs alongside the NDIF **API** and **queue** services. You *can* spin it up on its own for development, but most functionality (job submission & orchestration) requires the API.

---

## How NDIF uses this service

- **Cluster management**: Starts/connects to a Ray cluster (head/client), tracks nodes/resources, surfaces health and metrics.
- **Serve deployments**: Registers and scales Ray Serve apps that execute NDIF tasks.
- **Inter-service glue**: Uses environment variables to talk to NDIF’s API, object store (MinIO/S3), metrics (InfluxDB/Prometheus), and logs (Loki).
- **Observability**: Exposes Prometheus metrics and ships logs/traces via configured exporters.

---

## Directory structure

src/services/ray/
├── Dockerfile
├── environment.yml # Conda/pip environment for the Ray service
├── start.sh # Entrypoint script invoked by Compose
├── config/
│ └── ray_config.yml # (mounted via compose) Ray config
├── deployments/
│ └── controller/
│ ├── controller.py # Controller (Serve orchestration)
│ └── cluster/
│ ├── cluster.py # Cluster abstraction (lifecycle/state/scale)
│ └── node.py # Node model (ID/resources/health)
├── logutil/
│ └── logger.py # Loki/stdout logging shim
├── metrics/
│ └── prometheus.py # Prometheus metric helpers/exporters
├── providers/
│ └── object_store.py # MinIO/S3 helpers (if used)
├── types.py # Shared constants/enums/keys
└── main.py # Optional dev entrypoint


---

## Main classes

| Class        | Location                                  | Description |
|--------------|-------------------------------------------|-------------|
| **Controller** | `deployments/controller/controller.py`     | Orchestrates Ray Serve deployments (create/update/scale), wires NDIF config into the app graph. |
| **Cluster**    | `deployments/controller/cluster/cluster.py` | Abstraction over Ray cluster state (head address, nodes, resources), readiness checks & scaling helpers. |
| **Node**       | `deployments/controller/cluster/node.py`    | Node identity and resource state (CPU/GPU/host ID) used by Cluster/Controller for placement/health. |

---

## Dependencies (from `environment.yml`)

`ndif/src/services/ray/environment.yml` (Ray-specific environment) includes:

- **ray[serve]==2.47.0** — distributed execution + Ray Serve.
- **prometheus_client** — metrics exporter used by `metrics/prometheus.py`.
- **python-logging-loki** — optional Loki handler used by `logutil/logger.py` (shimmed to stdout if Loki is unavailable).
- **boto3** — S3/MinIO access (object store).
- **influxdb-client** — write operational metrics/series to InfluxDB (as configured in compose).
- **nnsight** — project-specific; keep only if workloads under Ray actually import it.
- **python-slugify** — utility; keep if referenced by deployments/config.
- **google-api-python-client** — Google Calendar scheduling (see env vars below for creds / calendar id).

> ⚠️ `environment.yml` currently ends with `- google` (likely accidental/incomplete). Replace this with the specific Google packages you need (e.g., `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`) **or remove it** if unused.

**Suggested cleanup** (only if unused in Ray service code):
- If the Ray service does **not** import `nnsight` directly (only tasks imported at runtime do), move `nnsight` to the repo’s workload environment or keep it documented as optional.
- Ensure the minimal set here is installable headless in the Ray image.

---

## Environment variables (as used by NDIF + Ray)

These are defined in **`ndif/compose/dev/.env`** and wired in **`ndif/compose/dev/docker-compose.yml`**. The Ray container consumes the following directly (see the `ray:` service `environment:` block):

| Variable | Purpose |
|---|---|
| `LOKI_URL` | Push URL for Loki logs (e.g., `http://${HOST_IP}:${DEV_LOKI_PORT}/loki/api/v1/push`). |
| `OBJECT_STORE_URL` | MinIO/S3 endpoint used by workloads (e.g., `${HOST_IP}:${DEV_MINIO_PORT}`). |
| `API_URL` | Base URL of NDIF API (e.g., `http://${HOST_IP}:${DEV_API_PORT}`). |
| `INFLUXDB_ADDRESS` | InfluxDB HTTP endpoint (e.g., `http://${HOST_IP}:${DEV_INFLUXDB_PORT}`). |
| `INFLUXDB_ADMIN_TOKEN`, `INFLUXDB_ORG`, `INFLUXDB_BUCKET` | InfluxDB auth and target. |
| `SCHEDULING_GOOGLE_CALENDAR_ID` | Calendar ID for scheduling. |
| `SCHEDULING_GOOGLE_CREDS_PATH` | Path to Google API creds inside the container (mounted file). |

Other **Ray-related** variables from `.env` that shape the deployment:

| Variable | Purpose |
|---|---|
| `N_DEVICES` | Number of NVIDIA GPU devices passed through to the container (Compose `deploy.resources.reservations.devices`). |
| `HOST_IP` | Used to construct service URLs passed into containers. |
| `RAY_DASHBOARD_HOST` | Dashboard bind host (e.g., `0.0.0.0`). |
| `RAY_METRICS_GAUGE_EXPORT_INTERVAL_MS` | Interval for Ray metrics gauge export (ms). |
| `RAY_SERVE_QUEUE_LENGTH_RESPONSE_DEADLINE_S` | Deadline for Serve queue length responses. |

**Ports (host ↔ container) from `.env` and Compose:**

| Purpose | Host Port | Container Port |
|---|---:|---:|
| Ray head (redis/gcs) | `${DEV_RAY_HEAD_PORT}` (= **6380**) | `${RAY_HEAD_INTERNAL_PORT}` (= **6379**) |
| Ray client (ray://) | `${DEV_RAY_CLIENT_PORT}` (= **9998**) | `${RAY_CLIENT_INTERNAL_PORT}` (= **10001**) |
| Ray dashboard (HTTP) | `${DEV_RAY_DASHBOARD_PORT}` (= **8266**) | `${RAY_DASHBOARD_INTERNAL_PORT}` (= **8265**) |
| Ray Serve HTTP | `${DEV_RAY_SERVE_PORT}` (= **8267**) | `${RAY_SERVE_INTERNAL_PORT}` (= **8267**) |

> The **API** and **queue** services connect to the Ray client via:  
> `RAY_ADDRESS=ray://${HOST_IP}:${DEV_RAY_CLIENT_PORT}` (see `docker-compose.yml`).

---

## Spin up the Ray service

> You can start the Ray container alone, but without the **API** (and often **queue**) it won’t receive real jobs.

### With Docker Compose (recommended)

```bash
cd ndif/compose/dev
docker compose up ray
# or to run the whole stack:
docker compose up

## Dev shell, exec into the running container:

```bash
docker compose exec ray bash

