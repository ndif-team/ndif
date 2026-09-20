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

deployments/

The ray/deployments package provides the deployment orchestration layer of NDIF’s Ray service.
It manages how models are deployed, cached, scaled, and scheduled across Ray clusters.

Key Components

deployments/controller/ – The orchestration core that manages deployments across nodes.

controller.py
Defines the Controller component responsible for orchestrating and managing model deployments across Ray nodes.
It coordinates model lifecycle operations (deploy, cache, delete) and maintains consistent cluster state via periodic synchronization with Ray Serve.

cluster/ – Handles node discovery, resource tracking, model placement, and deployment deltas.

cluster.py
Implements the Cluster management layer. It monitors Ray nodes, evaluates model placement, and schedules model deployments across the distributed cluster based on GPU memory, model size, and caching strategy.

node.py
Defines the Node-level resource and deployment management layer.
Represents an individual Ray node (with GPU and CPU memory resources) and encapsulates logic for evaluating, deploying, evicting, and caching models locally.

evaluator.py
Implements the model size evaluator and metadata cache.
Estimates GPU memory requirements for models prior to deployment and stores those evaluations for reuse across scheduling decisions.

deployment.py
Defines the deployment abstraction layer for NDIF’s Ray service.
Represents a single model instance running on the Ray cluster, managing its lifecycle (create, cache, restart, delete) and metadata such as resource usage, temperature level, and deployment timing.

gcal/ – Adds calendar-driven scheduling via Google Calendar.

scheduler.py
Defines the SchedulingActor, a Ray-based background process that integrates Google Calendar events with NDIF’s model deployment system.
It continuously monitors a configured calendar and triggers model deployment actions via the controller based on scheduled events.

deployments/modeling/ – Defines how individual model replicas are loaded, executed, cached, and monitored.

base.py
Implements the runtime for model replicas (Ray actors) and the request-execution pipeline.

util.py
Provides utilities for Accelerate hook cleanup, HF cache management, and thread control.

distributed/

The ray/distributed package is the low-level distributed runtime layer of NDIF Ray.
It equips the system to:

Efficiently distribute large models across GPUs

Load weights in parallel from HF caches

Coordinate tensor parallel computation for transformer layers

Remain compatible with NNsight instrumentation and Ray-based orchestration

Key Components

parallel_dims.py – Defines the parallelism configuration used for distributed runs.

util.py – Utilities for loading sharded HF weights, DTensor intervention support, and patching Accelerate.

tensor_parallelism/test.py – Smoke test and example script for TP runs with NNsight.

tensor_parallelism/plans/llama.py – Provides a Tensor Parallel plan for LLaMA-style transformer models.

nn/

The ray/nn package defines the secure, sandboxed neural network execution layer of the NDIF Ray service.
It enables safe, remote model execution through NNsight’s tracing backend while enforcing strict import and runtime protections to isolate untrusted or dynamic model code.

Key Components

backend.py – Implements the remote execution backend for traced model runs.

ops.py – Defines a utility for capturing and redirecting model logs.

security/protected_environment.py – Implements runtime sandboxing and import whitelisting to prevent unsafe code execution.

security/protected_object.py – Provides object-level security wrappers to prevent tampering with model components after load.

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