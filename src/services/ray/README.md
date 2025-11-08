# NDIF Ray Service

The **Ray service** is NDIF’s distributed execution and inference layer.  
It manages parallel workloads, model deployments, and inter-service coordination between NDIF’s API, queue, and telemetry systems.  
Built on [Ray](https://docs.ray.io/en/latest/), it provides a flexible and horizontally scalable runtime for distributed model execution and monitoring.

---

## 📘 Overview

The Ray service powers NDIF’s distributed computation by:

- **Spawning and managing a Ray cluster** (head + workers) to handle distributed jobs.  
- **Deploying and scaling models** via Ray Serve and NDIF’s internal controller framework.  
- **Handling orchestration, evaluation, and scheduling**, including integrations like Google Calendar scheduling.  
- **Emitting structured logs and metrics** for centralized observability via Loki, InfluxDB, and Prometheus.  

It runs as one of NDIF’s main services (alongside `api`, `queue`, and telemetry containers) and can be started independently for testing.

---

## 📁 Directory structure

src/services/ray/
├── environment.yml
├── README.md
├── start.sh
├── start-worker.sh
└── src/
├── init.py
├── logging/ # Logging utilities (Loki/stdout shims)
├── metrics/ # Prometheus exporters and metric helpers
├── providers/ # External data providers (e.g., object store)
├── schema/ # Data schema definitions (Pydantic models)
├── types.py # Shared constants and enums
└── ray/
├── init.py
├── resources.py # Resource and device reporting utilities
├── config/
│ └── ray_config.yml # Ray runtime configuration
├── deployments/
│ ├── init.py
│ ├── controller/
│ │ ├── init.py
│ │ ├── controller.py # Orchestrates Ray Serve deployments
│ │ ├── cluster/
│ │ │ ├── init.py
│ │ │ ├── cluster.py # Manages cluster state and scaling
│ │ │ ├── deployment.py # Deployment-level representation
│ │ │ ├── evaluator.py # Evaluation hooks and validation
│ │ │ └── node.py # Node model (resources, identity, health)
│ │ └── gcal/
│ │ ├── init.py
│ │ ├── controller.py # Calendar scheduling controller
│ │ └── scheduler.py # Scheduling logic (Google Calendar API)
│ └── modeling/
│ ├── init.py
│ ├── base.py # Base model abstractions
│ ├── model.py # Model runner definitions
│ └── util.py # Modeling utilities
├── distributed/
│ ├── init.py
│ ├── parallel_dims.py # Tensor and data parallel utilities
│ ├── util.py # Distributed execution helpers
│ └── tensor_parallelism/
│ ├── init.py
│ ├── test.py # Test harness for tensor parallel plans
│ └── plans/
│ ├── init.py
│ └── llama.py # Tensor parallel plan for LLaMA-like models
└── nn/
├── init.py
├── backend.py # Execution backend abstraction
├── ops.py # Core NN ops distributed over Ray
├── sandbox.py # Experimental NN components
└── security/
├── init.py
├── protected_environment.py # Sandboxed exec environment
└── protected_object.py # Safe wrappers for model/data objects


---

## 🧩 Main classes and modules

| Component | Path | Description |
|------------|------|-------------|
| **Controller** | `ray/deployments/controller/controller.py` | Orchestrates Ray Serve deployments; handles start, scale, and teardown of NDIF workloads. |
| **Cluster** | `ray/deployments/controller/cluster/cluster.py` | Abstraction over Ray cluster lifecycle, node registration, and scaling logic. |
| **Node** | `ray/deployments/controller/cluster/node.py` | Represents an individual Ray node, including ID, resources, and health. |
| **Evaluator** | `ray/deployments/controller/cluster/evaluator.py` | Evaluates deployments and validates cluster configuration. |
| **Deployment** | `ray/deployments/controller/cluster/deployment.py` | Internal model describing deployment state and metadata. |
| **GCalController** | `ray/deployments/controller/gcal/controller.py` | Integrates Google Calendar scheduling for timed deployments or evaluations. |
| **GCalScheduler** | `ray/deployments/controller/gcal/scheduler.py` | Implements Google Calendar API logic and scheduling callbacks. |
| **Modeling** | `ray/deployments/modeling/*` | Defines base and derived model wrappers for Ray Serve tasks. |
| **Distributed utilities** | `ray/distributed/*` | Manages parallelism and tensor-parallel plans (especially `plans/llama.py`). |
| **NN backend and ops** | `ray/nn/backend.py`, `ray/nn/ops.py` | Provides neural network execution primitives under Ray. |
| **Protected environment** | `ray/nn/security/protected_environment.py` | Safeguards execution within sandboxed environments. |
| **Protected object** | `ray/nn/security/protected_object.py` | Wraps sensitive objects with restricted access controls. |

---

## ⚙️ Dependencies (from `environment.yml`)

| Package | Purpose |
|----------|----------|
| `ray[serve]==2.47.0` | Core distributed compute and serving backend. |
| `prometheus_client` | Metric exporter for Grafana dashboards. |
| `python-logging-loki` | Loki log exporter (shimmed by `src/logging`). |
| `boto3` | Access to MinIO/S3 object stores. |
| `influxdb-client` | Write operational metrics to InfluxDB. |
| `google-api-python-client` | Integrates Google Calendar for scheduling. |
| `nnsight` | Used for NDIF model interpretability or inspection tasks (remove if unused). |
| `python-slugify` | Utility for slugging model or deployment names. |

> ⚠️ Remove the dangling `- google` entry at the bottom of `environment.yml` or replace it with specific Google libraries (`google-auth`, `google-auth-oauthlib`, etc.) actually imported in the source.

---

## 🌍 Environment variables (from NDIF Compose)

| Variable | Purpose |
|-----------|----------|
| `LOKI_URL` | URL for pushing logs to Loki. |
| `OBJECT_STORE_URL` | MinIO/S3 object store endpoint. |
| `API_URL` | URL of the NDIF API service. |
| `INFLUXDB_ADDRESS` / `INFLUXDB_*` | Metrics destination (InfluxDB connection, org, bucket, token). |
| `SCHEDULING_GOOGLE_CALENDAR_ID` | ID of the Google Calendar used for scheduling. |
| `SCHEDULING_GOOGLE_CREDS_PATH` | Path to the credentials file inside the Ray container. |
| `HOST_IP` | Host machine IP used to build service URLs. |
| `N_DEVICES` | Number of GPUs allocated to the Ray service container. |
| `RAY_DASHBOARD_HOST` | Bind address for the Ray Dashboard. |
| `RAY_METRICS_GAUGE_EXPORT_INTERVAL_MS` | Metric export interval (ms). |
| `RAY_SERVE_QUEUE_LENGTH_RESPONSE_DEADLINE_S` | Response timeout for Serve queue metrics. |

**Port mapping:**

| Service | Host Port | Container Port |
|----------|------------|----------------|
| Ray head | 6380 | 6379 |
| Ray client (`ray://`) | 9998 | 10001 |
| Ray dashboard | 8266 | 8265 |
| Ray Serve HTTP | 8267 | 8267 |

---

## 🚀 Spinning up the Ray service

### Option 1 — via Docker Compose (recommended)

```bash
cd ndif/compose/dev
docker compose up ray

### Option 2 — stand-alone (for development)

```bash
export $(grep -v '^#' compose/dev/.env | xargs)
python -m ray.src.main


⚠️ Without the API and queue services, the Ray container will run but cannot process NDIF workloads.

🧠 Notes

The Ray service emits traces via OpenTelemetry and exposes metrics for Prometheus scraping.

Jaeger tracing identifies this service under Service = ray.

Logs flow to Grafana Loki with label {service="ray"}.

start-worker.sh is used to launch additional Ray workers from the same image when scaling horizontally.