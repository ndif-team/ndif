# NDIF Ray Service

The **Ray Service** provides the distributed execution layer for NDIF.  
It manages deployments, task scheduling, and inter-service communication between NDIF’s core components (API, queue, telemetry, etc.) using [Ray](https://docs.ray.io/).

This service defines **controller**, **cluster**, and **node** abstractions that wrap Ray’s internal APIs into a structured, typed interface compatible with NDIF’s deployment system.

---

## 📘 Overview

The Ray service enables NDIF to:
- Launch and manage distributed inference or compute tasks.
- Maintain cluster state (nodes, actors, replicas).
- Register and monitor deployments via Ray Serve.
- Communicate with the NDIF API service through shared environment variables and telemetry hooks.

---

## 📁 Directory Structure

src/services/ray/
├── Dockerfile
├── compose/
│ └── dev/
│ ├── docker-compose.yml
│ └── .env
├── src/
│ └── ray/
│ ├── init.py
│ ├── deployments/
│ │ ├── init.py
│ │ └── controller/
│ │ ├── controller.py # Main Controller class
│ │ └── cluster/
│ │ ├── cluster.py # Cluster abstraction
│ │ ├── node.py # Node representation
│ │ └── init.py
│ ├── logutil/
│ │ └── logger.py # Centralized Loki / stdout logging shim
│ ├── metrics/
│ │ └── prometheus.py # Prometheus metrics helpers
│ ├── providers/
│ │ └── object_store.py # Object store access utilities
│ ├── types.py # Shared constants and enums
│ └── main.py # Entrypoint for Ray worker process
└── README.md


**Highlights:**
- `controller.py`: orchestrates deployments, interacts with Ray Serve.
- `cluster.py`: manages groups of nodes, scaling, and lifecycle.
- `node.py`: encapsulates node identity, state, and resource reporting.
- `logger.py`: handles logging (stdout + Loki when configured).
- `metrics/`: exposes Prometheus metrics for monitoring.
- `providers/`: supports NDIF’s provider interface (e.g., object storage).

---

## 🧩 Main Classes

| Class | Location | Description |
|-------|-----------|-------------|
| **Controller** | `deployments/controller/controller.py` | Manages Ray Serve deployments, handles start/stop and configuration propagation. |
| **Cluster** | `deployments/controller/cluster/cluster.py` | Represents the Ray cluster, including node registration and scaling logic. |
| **Node** | `deployments/controller/cluster/node.py` | Encapsulates per-node metadata and runtime information (CPU, GPU, host ID). |
| **ServeSubmissionClient (mocked)** | Internal Ray import | Used to submit and control deployments via Ray Dashboard (mocked for Sphinx builds). |

---

## 📚 Dependencies

Core libraries used by the Ray service:

| Library | Purpose |
|----------|----------|
| **ray** | Distributed compute and serving framework. |
| **pydantic** | Data validation and model definition for config and schema objects. |
| **fastapi** | Lightweight HTTP API interface (used indirectly by Ray Serve). |
| **prometheus_client** | Exposes metrics endpoints for Grafana/Prometheus. |
| **opentelemetry-sdk / opentelemetry-exporter-otlp** | Tracing and observability. |
| **logging-loki** | Optional Loki handler for centralized logging. |
| **python-dotenv** | Loads `.env` configuration when running locally. |

> ✅ When updating dependencies, also clean unused packages from  
> `environment.yml` and verify the Ray service builds correctly via Docker Compose.

---

## 🌍 Environment Variables

| Variable | Defined In | Purpose |
|-----------|-------------|----------|
| `HOST_IP` | `.env`, `docker-compose.yml` | Advertised IP address of the host for inter-service communication. |
| `N_DEVICES` | `.env` | Number of worker devices or Ray nodes to start. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `.env` | OpenTelemetry collector endpoint. |
| `RAY_ADDRESS` | `docker-compose.yml` | Ray cluster head address (used for client connection). |
| `RAY_DASHBOARD_HOST` | `docker-compose.yml` | Enables Ray Dashboard access. |
| `SERVICE_NAME` | `docker-compose.yml` | Used by tracing/logging to tag the service (e.g., “ray”). |
| `LOKI_URL` | `.env` | URL to Loki instance for log aggregation. |
| `PROMETHEUS_PORT` | `.env` | Port for Prometheus metrics exporter. |

Check `compose/dev/docker-compose.yml` and `compose/dev/.env` for current values.

---

## 🚀 Running the Ray Service

You can run the Ray service independently, but it **requires the NDIF API service** to communicate fully.

###1️From Docker Compose

```bash
cd compose/dev
docker compose up ray

### 2 Local Python (dev only)
```bash
# Load environment for local run
export $(grep -v '^#' compose/dev/.env | xargs)

# Start the local Ray entrypoint (adjust path if needed)
python -m ray.src.main
