# NDIF: Design and Implementation

*Jaden Fiotto-Kaufman*

---

## Goal of This Document

This document provides an overview of the design choices and implementation details of NDIF (National Deep Inference Fabric). Its purpose is to serve as a **source of truth** for understanding how NDIF works internally, enabling developers and contributors to reason correctly about its behavior.

This is the long-form design doc, intended for humans ramping up on the system. Agents should read `CLAUDE.md` at the repo root, which is shorter and task-oriented. Both files are kept in sync with the code; when they disagree, the code is authoritative.

---

## Quick Navigation

If you already know what you're looking for, jump directly to the relevant section. The rest of this document is depth-first and assumes you want to read it in order.

### ⚠️ Before you edit anything

**Security sandbox (`src/ndif/services/ray/nn/security/`)** is the highest-stakes area in the repo. User-submitted code runs there; a regression creates a sandbox escape. Before touching any file under that directory, read **[§7](#7-security)** in full *and* the directory's own `README.md`. Then run `pytest tests/test_security_guards.py --run-remote` against a live stack. Do not cargo-cult a pattern from elsewhere in the codebase into security code without understanding why each of the six layers exists.

**Invariants that look wrong but aren't** are collected in **[§14](#14-invariants)**. If you are about to "fix" something that seems over-complicated — especially `num_gpus=0` on the ModelActor, the two whitelists, or the `build()/apply()` ordering — check §14 first. Several things in this codebase were the second attempt and exist to dodge a specific failure mode.

### At a glance: a request in ten steps

This is the same diagram as [§2.3](#23-data-flow), promoted here so it's the first thing you see.

```
Client (nnsight)
  │  POST /request  (serialized intervention, model-key, api-key headers)
  ▼
API  (FastAPI + Gunicorn)                                        §3
  │  validate → BackendRequestModel → RedisProvider.lpush("queue")
  ▼
Redis ("queue")
  │  brpop (10s) + batch rpop up to 32
  ▼
Dispatcher (lives in API process, Ray client)                    §4.1
  │  route by model_key to a per-model Processor
  ▼
Processor (one per model, async worker)                          §4.2
  │  PROVISIONING → DEPLOYING → READY ↔ BUSY
  │  calls Controller.deploy({model_key: DeploymentConfig(...)})
  ▼
Controller (Ray head node actor)                                 §5.1
  │  Cluster.deploy() → picks best node, may evict              §5.2
  │  build() → DeploymentDelta                                   §5.6
  │  apply(): delete → cache → from_cache → create   ⚠️ ordered
  ▼
ModelActor (per model, per node)                                 §6
  │  pre()    — deserialize under Protector(DESERIALIZATION)     §6.2
  │  execute()— run user code under Protector(EXECUTION) in     §6.3
  │             a thread (autocast, StdoutRedirect)
  │  post()   — BackendResultModel.save() to MinIO, COMPLETED    §6
  │  cleanup()— Globals.clear(), clear_set_attrs(), GC/5        §6.5
  ▼
Socket.IO → Client downloads result from MinIO presigned URL     §8.5
```

### Task → files cookbook

The "what file do I change for X" table.

| I want to change… | Start here |
|---|---|
| An API endpoint or HTTP validation | `src/ndif/services/api/app.py`, `dependencies.py` — §3 |
| Request routing / queue behavior | `src/ndif/services/api/queue/dispatcher.py`, `processor.py` — §4 |
| What happens when a model gets scheduled | `src/ndif/services/ray/deployments/controller/cluster/{cluster,node,evaluator}.py` — §5.2–5.5 |
| Model HOT/WARM/COLD transitions | `src/ndif/services/ray/deployments/controller/cluster/deployment.py`, `controller.py::apply()` — §5.6 |
| Model execution, timeouts, cleanup | `src/ndif/services/ray/deployments/modeling/base.py` — §6 |
| ⚠️ The sandbox (anything security) | `src/ndif/services/ray/nn/security/` — §7 (read README.md first) |
| What modules user code can import | `src/ndif/services/ray/nn/security/whitelist.yaml` — §7.11 (requires Ray image rebuild) |
| Result/response serialization and storage | `src/ndif/common/schema/{request,response,result,mixins}.py`, `providers/objectstore.py` — §8 |
| Google Calendar scheduling | `src/ndif/services/ray/deployments/controller/gcal/` — §5.7 |
| Distributed tracing / Jaeger spans | `src/ndif/common/tracing/`, `src/ndif/services/api/tracing/` — §10.4 |
| API keys / dev-mode Postgres | `src/ndif/services/api/db.py`, `src/ndif/common/providers/postgres.py`, `docker/postgres/init.sql` — §9.4 |
| A CLI command | `cli/commands/` — §11 |
| Env-var default or new config knob | `.env.example` + the relevant service `config.py` — §15 appendix |
| Docker build or compose wiring | `docker/Dockerfile`, `docker/docker-compose.yml`, `Makefile` — §12.1 |
| The standalone uptime monitor | `src/ndif/services/monitor/` — §13 |

### File index

A one-page map of the important files. Use this if you know the topic but not the filename.

| Path | What lives here |
|---|---|
| `src/ndif/services/api/app.py` | FastAPI endpoints: `/request`, `/response/{id}`, `/status`, `/env`, `/connected`, `/ping` |
| `src/ndif/services/api/dependencies.py` | `validate_request` — api-key / version / hotswap checks |
| `src/ndif/services/api/db.py` | `AccountsDB` wrapper + `NDIF_DEV_MODE` bypass |
| `src/ndif/services/api/config.py` | `AppConfig` — API env vars |
| `src/ndif/services/api/queue/dispatcher.py` | `Dispatcher` — reads Redis queue, routes to Processors, manages Ray connection |
| `src/ndif/services/api/queue/processor.py` | `Processor` — per-model state machine (`PROVISIONING → DEPLOYING → READY ↔ BUSY`) |
| `src/ndif/services/api/queue/util.py` | Ray client deadlock `patch()`, `controller_handle()`, `submit()` |
| `src/ndif/services/ray/deployments/controller/controller.py` | `_ControllerActor`, `build()`, `apply()`, `deploy()`, `evict()`, `flush_warm_cache()`, `status()`, `env()` |
| `src/ndif/services/ray/deployments/controller/cluster/cluster.py` | `Cluster` — multi-node state, deploy/evict orchestration |
| `src/ndif/services/ray/deployments/controller/cluster/node.py` | `Node` — single node's GPUs/CPU/cache; `evictions()` algorithm |
| `src/ndif/services/ray/deployments/controller/cluster/deployment.py` | `Deployment` + `DeploymentLevel` enum |
| `src/ndif/services/ray/deployments/controller/cluster/evaluator.py` | `ModelEvaluator` — meta-model size + padding |
| `src/ndif/services/ray/deployments/controller/gcal/controller.py` | `SchedulingControllerActor` — Controller subclass that pulls deployments from Google Calendar |
| `src/ndif/services/ray/deployments/controller/gcal/scheduler.py` | `SchedulingActor` — actual calendar poll loop |
| `src/ndif/services/ray/deployments/modeling/base.py` | `BaseModelDeployment`, `ModelActor`, `pre()`/`execute()`/`post()`/`cleanup()` |
| `src/ndif/services/ray/deployments/modeling/util.py` | `kill_thread()`, `load_with_cache_deletion_retry()`, `remove_accelerate_hooks()` |
| `src/ndif/services/ray/nn/backend.py` | `RemoteExecutionBackend` — bridge between NNsight and the Protector |
| `src/ndif/services/ray/nn/ops.py` | `StdoutRedirect` — print-capture during execution |
| `src/ndif/services/ray/nn/security/protector.py` | `Protector` — orchestrates the six sandbox layers |
| `src/ndif/services/ray/nn/security/importer.py` | `Importer`, `SandboxFinder`, `ProtectedModule`, `UnauthorizedModule` |
| `src/ndif/services/ray/nn/security/guards.py` | `guarded_getattr`, `SAFE_BUILTINS`, audit hook, `restricted_compile/exec` |
| `src/ndif/services/ray/nn/security/protected_objects.py` | `ProtectedObject`, `protect()`, `clear_set_attrs()` |
| `src/ndif/services/ray/nn/security/whitelist.yaml` | Policy: allowed modules, allowed builtins, blocked submodules, dunder lists |
| `src/ndif/common/schema/request.py` | `BackendRequestModel` — `from_request`, `deserialize`, `create_response` |
| `src/ndif/common/schema/response.py` | `BackendResponseModel` — `respond()` (Socket.IO / callback / save) |
| `src/ndif/common/schema/result.py` | `BackendResultModel` + `TensorStoragePickler` |
| `src/ndif/common/schema/mixins.py` | `ObjectStorageMixin`, `TelemetryMixin` |
| `src/ndif/common/schema/deployment_config.py` | `DeploymentConfig` (dedicated, timeouts) |
| `src/ndif/common/providers/redis.py` | `RedisProvider` — sync + async clients |
| `src/ndif/common/providers/objectstore.py` | `ObjectStoreProvider` — MinIO/S3 via boto3 |
| `src/ndif/common/providers/socketio.py` | `SioProvider` |
| `src/ndif/common/providers/postgres.py` | `PostgresProvider` — connection pool |
| `src/ndif/common/providers/mailgun.py` | `MailgunProvider` — email notifications (gcal errors, non-blocking callbacks) |
| `src/ndif/common/providers/ray.py` | `RayProvider` — Ray client connection |
| `src/ndif/common/tracing/setup.py` | `init_tracing()`, OTLP exporter |
| `src/ndif/common/tracing/spans.py` | `trace_span()`, `set_request_attributes()` |
| `src/ndif/common/tracing/context.py` | `TracingContext` — inject/extract for cross-process propagation |
| `src/ndif/common/metrics/*.py` | `GPUMemMetric`, `ExecutionTimeMetric`, etc. — all write to InfluxDB |
| `src/ndif/common/types.py` | `MODEL_KEY`, `API_KEY`, `SESSION_ID`, `REQUEST_ID` |
| `cli/cli.py` | Click entry point |
| `cli/commands/*.py` | One file per `ndif <command>` |
| `cli/lib/session.py` | `SessionConfig`, `~/.ndif/` layout |
| `cli/lib/checks.py` | Pre-flight checks for `ndif start` |
| `cli/lib/deps.py` | Redis/MinIO micromamba bootstrap |
| `docker/Dockerfile` | Multi-purpose — `ARG NAME=api` or `NAME=ray` |
| `docker/docker-compose.yml` | Full stack orchestration |
| `docker/postgres/init.sql` | Dev-mode keys DB + test key |
| `Makefile` | `build`, `up`, `down`, `ta`; resolves `NNSIGHT_PATH` for the compose bind mount |
| `.env.example` | Default env vars (loaded by Makefile + compose) |
| `src/ndif/services/monitor/` | Standalone uptime monitor — runs outside the stack |
| `telemetry/grafana/dashboards/` | Pre-built Grafana dashboards |
| `telemetry/prometheus/prometheus.yml` | Prometheus scrape config |
| `tests/conftest.py` | Remote-test skip logic (`--run-remote` gate) |
| `tests/test_nnsight.py`, `test_security_guards.py`, `test_user_code.py`, `test_hotswapping.py` | End-to-end test suites |

---

## Table of Contents

1. [Introduction](#1-introduction)
   - [What NDIF Does](#what-ndif-does)
   - [Design Principles](#design-principles)
   - [System Overview](#system-overview)
2. [Architecture](#2-architecture)
   - [Overview](#overview)
   - [Service Boundaries](#21-service-boundaries)
   - [Communication Patterns](#22-communication-patterns)
   - [Data Flow](#23-data-flow)
3. [API Service](#3-api-service)
   - [Overview](#overview-1)
   - [FastAPI Application](#31-fastapi-application)
   - [Request Validation](#32-request-validation)
   - [Endpoints](#33-endpoints)
   - [WebSocket Communication](#34-websocket-communication)
   - [Configuration](#35-configuration)
   - [Iterating on this subsystem](#36-iterating-on-this-subsystem)
4. [Queue System](#4-queue-system)
   - [Overview](#overview-2)
   - [The Dispatcher](#41-the-dispatcher)
   - [The Processor](#42-the-processor)
   - [Request Lifecycle](#43-request-lifecycle)
   - [Error Handling and Recovery](#44-error-handling-and-recovery)
   - [Iterating on this subsystem](#45-iterating-on-this-subsystem)
5. [Ray Service](#5-ray-service)
   - [Overview](#overview-3)
   - [The Controller](#51-the-controller)
   - [Cluster Management](#52-cluster-management)
   - [Model Evaluation](#53-model-evaluation)
   - [Deployment Levels](#54-deployment-levels)
   - [Deployment Scheduling](#55-deployment-scheduling)
   - [The Build/Apply Cycle](#56-the-buildapply-cycle)
   - [Google Calendar Scheduling](#57-google-calendar-scheduling)
   - [Iterating on this subsystem](#58-iterating-on-this-subsystem)
6. [Model Execution](#6-model-execution)
   - [Overview](#overview-4)
   - [The ModelActor](#61-the-modelactor)
   - [Request Deserialization](#62-request-deserialization)
   - [Execution Pipeline](#63-execution-pipeline)
   - [Timeout and Cancellation](#64-timeout-and-cancellation)
   - [Cleanup and Memory Management](#65-cleanup-and-memory-management)
   - [Streaming and Logging](#66-streaming-and-logging)
   - [Iterating on this subsystem](#67-iterating-on-this-subsystem)
7. [Security](#7-security)
   - [Overview](#overview-5)
   - [The Protector (orchestrator)](#71-the-protector-orchestrator)
   - [Layer 1: Import interception](#72-layer-1-import-interception-importerpy)
   - [Layer 2: Meta-path finder](#73-layer-2-meta-path-finder-importerpy--sandboxfinder)
   - [Layer 3: Deserialization hardening](#74-layer-3-deserialization-hardening-protectorpy)
   - [Layer 4: Builtin restriction](#75-layer-4-builtin-restriction-whitelistpy-guardspy)
   - [Layer 5: Attribute guards](#76-layer-5-attribute-guards-guardspy)
   - [Layer 6: Audit hook](#77-layer-6-audit-hook-guardspy)
   - [Module Immutability](#78-module-immutability-protectedmodule)
   - [Protected Objects](#79-protected-objects-protected_objectspy)
   - [Restricted Compile and Exec](#710-restricted-compile-and-exec-guardspy)
   - [The Whitelist](#711-the-whitelist)
   - [Request Lifecycle Through the Sandbox](#712-request-lifecycle-through-the-sandbox)
   - [Iterating on this subsystem](#713-iterating-on-this-subsystem)
8. [Schema and Data Models](#8-schema-and-data-models)
   - [Overview](#overview-6)
   - [BackendRequestModel](#81-backendrequestmodel)
   - [BackendResponseModel](#82-backendresponsemodel)
   - [BackendResultModel](#83-backendresultmodel)
   - [Object Storage Mixin](#84-object-storage-mixin)
   - [Response Delivery](#85-response-delivery)
9. [Infrastructure and Providers](#9-infrastructure-and-providers)
   - [Overview](#overview-7)
   - [Redis](#91-redis)
   - [Object Store (MinIO/S3)](#92-object-store-minios3)
   - [Socket.IO](#93-socketio)
   - [PostgreSQL](#94-postgresql)
10. [Telemetry and Monitoring](#10-telemetry-and-monitoring)
    - [Overview](#overview-8)
    - [Metrics](#101-metrics)
    - [Logging](#102-logging)
    - [Grafana Dashboards](#103-grafana-dashboards)
    - [Distributed Tracing (OpenTelemetry → Jaeger)](#104-distributed-tracing-opentelemetry--jaeger)
11. [CLI](#11-cli)
    - [Overview](#overview-9)
    - [Session Management](#111-session-management)
    - [Commands](#112-commands)
    - [Worker Nodes](#113-worker-nodes)
12. [Deployment](#12-deployment)
    - [Overview](#overview-10)
    - [Docker](#121-docker)
    - [Native (CLI)](#122-native-cli)
    - [Configuration Reference](#123-configuration-reference)
13. [Monitor Service](#13-monitor-service)
    - [Overview](#overview-11)
    - [What it does](#131-what-it-does)
    - [Deployment (outside the stack)](#132-deployment-outside-the-stack)
    - [Dashboard](#133-dashboard)
    - [Configuration](#134-configuration)
14. [Invariants](#14-invariants) — ⚠️ read before "simplifying" anything
    - [`num_gpus=0` on ModelActor](#141-modelactor-is-declared-with-num_gpus0)
    - [Two whitelists](#142-two-whitelists-not-one)
    - [Stock `compile()`, not RestrictedPython AST](#143-compile-is-stock-python-not-restrictedpythons-ast-transform)
    - [`build()/apply()` ordering](#144-buildapply-ordering-is-delete--cache--from_cache--create)
    - [Ray client deadlock patch](#145-the-ray-client-deadlock-patch)
    - [`clear_set_attrs()` reverts writes](#146-clear_set_attrs-reverts-writes-instead-of-blocking-them)
    - [Audit hook is permanent per-process](#147-the-audit-hook-is-permanent-per-process)
    - [CUDA device-side assert → self-kill](#148-cuda-device-side-assertion-triggers-a-terminal-self-kill)
    - [Thread kills via `ctypes`](#149-thread-kills-use-ctypes-not-cooperative-cancellation)
    - [`UnauthorizedModule` defers errors](#1410-unauthorizedmodule-defers-errors-until-use)
    - [`whitelist.yaml` rebuilds the image](#1411-whitelistyaml-edits-require-an-image-rebuild)
    - [Eviction via string match](#1412-the-processor-detects-eviction-via-the-failed-to-look-up-actor-string)
    - [GC every 5 requests](#1413-garbage-collection-runs-every-5-requests-not-every-request)
    - [No `empty_cache()` in cleanup](#1414-cleanup-does-not-call-torchcudaempty_cache)
15. [Configuration Appendix](#15-configuration-appendix) — consolidated env-var reference

---

## 1. Introduction

### What NDIF Does

NDIF is the server infrastructure that powers remote execution for [NNsight](https://github.com/ndif-team/nnsight). When a researcher writes:

```python
model = LanguageModel("meta-llama/Llama-3.1-70B")

with model.trace("The Eiffel Tower is in", remote=True):
    hidden = model.model.layers[5].output[0].save()
```

That code doesn't execute locally. Instead:

1. NNsight serializes the intervention code and model specification into a request
2. The request is sent to NDIF's API server
3. NDIF deserializes the request, loads the model (or uses an already-loaded one), and executes the intervention
4. Results are serialized and sent back to the client

NDIF solves the problem of **democratizing access to large model internals**. Most researchers don't have access to the hardware required to run 70B+ parameter models. NDIF provides shared infrastructure where models are loaded once and serve many users, with each user's intervention code running in a secure sandbox.

### Design Principles

1. **Transparent to the user:** From NNsight's perspective, remote execution should behave identically to local execution. The same intervention code runs in both cases.

2. **Secure by default:** User-submitted code executes in a restricted environment. Users cannot access the filesystem, network, or modify the model weights. Only whitelisted modules and builtins are available.

3. **Resource-efficient:** Models are expensive to load. NDIF keeps models loaded across requests, manages GPU allocation across a cluster, and supports warm caching to CPU memory for fast reloading.

4. **Operationally simple:** A single `ndif start` command brings up the entire stack. The CLI manages sessions, service lifecycles, and provides monitoring commands.

### System Overview

NDIF consists of three primary services and one auxiliary service:

| Service | Role | Technology |
|---------|------|-----------|
| **API** | HTTP gateway, request validation, queue management | FastAPI, Gunicorn, Socket.IO |
| **Ray** | Distributed compute, model deployment, execution | Ray Actors |
| **CLI** | Service management, monitoring, deployment commands | Click |
| **Monitor** | Standalone uptime/latency dashboard + Discord alerts | Cron + Flask (§13) |

And several external dependencies:

| Dependency | Role |
|-----------|------|
| **Redis** | Request queue, pub/sub, Redis streams, Socket.IO backend |
| **MinIO** | S3-compatible object storage for results and responses |
| **PostgreSQL** | API key storage and tier management (dev-mode bypass available) |
| **Prometheus / InfluxDB / Grafana / Loki** | Metrics, logs, and dashboards |
| **Jaeger (OTLP)** | Distributed tracing across API ↔ Ray |

The project targets **Python 3.12+** and is packaged with [`uv`](https://github.com/astral-sh/uv) (see `pyproject.toml`). Any reference to Python 3.10 or conda-only setup in older docs is stale.

---

## 2. Architecture

### Overview

NDIF follows a queue-based architecture where the API service accepts requests, queues them in Redis, and a Dispatcher routes them to per-model Processors that coordinate with Ray Actors for execution.

### 2.1 Service Boundaries

```
┌─────────────────────────────────────────────────────────────────────┐
│                         API Service                                  │
│                                                                      │
│  ┌──────────┐   ┌─────────────┐   ┌─────────────────────────────┐  │
│  │ FastAPI   │   │ Validation  │   │ Dispatcher                  │  │
│  │ Endpoints │──>│ Middleware  │──>│                              │  │
│  │           │   │             │   │  ┌──────────┐ ┌──────────┐  │  │
│  └──────────┘   └─────────────┘   │  │Processor │ │Processor │  │  │
│                                    │  │(model A) │ │(model B) │  │  │
│                                    │  └──────────┘ └──────────┘  │  │
│                                    └─────────────────────────────┘  │
│                                                                      │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                        │
│  │  Redis   │   │  MinIO   │   │ Postgres │                        │
│  │ (Queue)  │   │ (Results)│   │ (Keys)   │                        │
│  └──────────┘   └──────────┘   └──────────┘                        │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              │ Ray Client Protocol
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         Ray Cluster                                  │
│                                                                      │
│  ┌──────────────────┐                                               │
│  │   Controller     │  (Head Node, Ray Actor)                       │
│  │   - Cluster mgmt │                                               │
│  │   - Scheduling   │                                               │
│  └──────────────────┘                                               │
│                                                                      │
│  ┌──────────────────┐  ┌──────────────────┐                        │
│  │   ModelActor     │  │   ModelActor     │  (Worker Nodes)         │
│  │   (model A)      │  │   (model B)      │                        │
│  │   - Execution    │  │   - Execution    │                        │
│  │   - Security     │  │   - Security     │                        │
│  └──────────────────┘  └──────────────────┘                        │
└─────────────────────────────────────────────────────────────────────┘
```

The API service and Ray cluster run as separate processes (or containers). They communicate via the Ray Client protocol. The Dispatcher within the API service connects to Ray as a client and submits method calls to Ray Actors.

### 2.2 Communication Patterns

NDIF uses several communication patterns depending on the interaction:

| Pattern | Used For | Implementation |
|---------|----------|----------------|
| **Request queue** | Client → Dispatcher | Redis list (`lpush`/`brpop`) |
| **RPC** | Dispatcher → Controller/ModelActor | Ray Client (`ray.call_remote`) |
| **Pub/Sub** | Status updates | Redis pub/sub channels |
| **Streams** | Dispatcher events (deploy, evict, kill) | Redis streams (`xadd`/`xread`) |
| **WebSocket** | Real-time client responses | Socket.IO via Redis manager |
| **Object store** | Large result payloads | MinIO (S3-compatible) |

**Why Redis for the queue instead of Ray's built-in task queue?**

The Dispatcher runs as part of the API service process, connected to Ray as a *client*. It needs to receive requests from the FastAPI endpoint (which runs in the same process) and route them. Redis provides a simple, reliable queue that decouples request receipt from processing, and also serves as the Socket.IO message broker for cross-process WebSocket communication.

### 2.3 Data Flow

A complete request flows through the system as follows:

```
Client (nnsight)
  │
  │  POST /request (HTTP)
  │  Headers: model-key, api-key, nnsight-version, python-version
  │  Body: serialized intervention code (pickled RequestModel)
  │
  ▼
FastAPI Endpoint
  │
  │  1. Validate API key (PostgreSQL lookup)
  │  2. Validate nnsight/python version compatibility
  │  3. Create BackendRequestModel from headers + body
  │  4. Push to Redis queue
  │  5. Return RECEIVED response via WebSocket
  │
  ▼
Redis Queue ("queue")
  │
  │  brpop (10s) + batch rpop up to 32 requests
  │
  ▼
Dispatcher
  │
  │  Route to Processor by model_key
  │  (create Processor if first request for this model)
  │
  ▼
Processor
  │
  │  1. Provision: Ask Controller to deploy model
  │  2. Initialize: Wait for ModelActor to be ready
  │  3. Execute: Submit request to ModelActor
  │
  ▼
Controller (Ray Actor)
  │
  │  1. Evaluate model size
  │  2. Find best node with available GPUs
  │  3. Evict other models if necessary
  │  4. Create ModelActor on selected node
  │
  ▼
ModelActor (Ray Actor)
  │
  │  1. Pre: Deserialize request in protected environment
  │  2. Execute: Run intervention code in sandbox
  │  3. Post: Serialize results, upload to MinIO
  │  4. Cleanup: Free memory, clear gradients
  │
  ▼
Client (nnsight)
  │
  │  Download results from MinIO presigned URL
  │  Deserialize into Python objects
```

---

## 3. API Service

### Overview

The API service is a FastAPI application served by Gunicorn with Uvicorn workers. It serves as the entry point for all client requests, handles validation, and hosts the Dispatcher which coordinates with the Ray cluster.

**Key files:**
- `src/ndif/services/api/app.py` — FastAPI application and endpoints
- `src/ndif/services/api/dependencies.py` — Request validation functions
- `src/ndif/services/api/config.py` — Environment-based configuration
- `src/ndif/services/api/db.py` — PostgreSQL API key store
- `src/ndif/services/api/queue/` — Dispatcher and Processor

### 3.1 FastAPI Application

The application is initialized in `app.py`:

```python
app = FastAPI()

# CORS middleware (permissive for client access)
app.add_middleware(CORSMiddleware, allow_origins=["*"], ...)

# Socket.IO manager backed by Redis (for cross-process communication)
socketio_manager = socketio.AsyncRedisManager(url=AppConfig.broker_url)
sm = SocketManager(app=app, mount_location="/ws", client_manager=socketio_manager, ...)
```

The Socket.IO manager uses Redis as a backend, which allows multiple API worker processes and the Dispatcher (which also emits Socket.IO events) to communicate with the same set of connected clients.

### 3.2 Request Validation

Every request to `/request` passes through the `validate_request` dependency, which performs four checks in sequence:

1. **API key authentication** — Looks up the key in PostgreSQL via `AccountsDB.api_key_exists()`. In dev mode (`NDIF_DEV_MODE=true`), all keys are accepted.

2. **NNsight version validation** — Compares the client's nnsight version (from the `nnsight-version` header) against the server's minimum. Rejects clients running older versions.

3. **Python version validation** — Compares the client's Python version (from the `python-version` header) against the server's minimum. Only major.minor versions are compared.

4. **Hotswapping access check** — Checks if the API key has the "hotswapping" tier, which allows deploying models that aren't in the dedicated schedule. This is stored in PostgreSQL's `key_tier_assignments` table.

```python
async def validate_request(raw_request: Request) -> BackendRequestModel:
    api_key = raw_request.headers.get("ndif-api-key", "")
    await authenticate_api_key(api_key)
    await validate_nnsight_version(raw_request.headers.get("nnsight-version", ""))
    await validate_python_version(raw_request.headers.get("python-version", ""))

    backend_request = BackendRequestModel.from_request(raw_request)
    backend_request.hotswapping = await check_hotswapping_access(api_key)
    return backend_request
```

### 3.3 Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/request` | POST | Submit an inference request. Validates, queues, and returns a RECEIVED response. |
| `/response/{id}` | GET | Retrieve the latest response for a request ID (for non-blocking polling). |
| `/status` | GET | Get cluster status (cached in Redis, fetched from Controller on cache miss). |
| `/env` | GET | Get Python environment info from the Ray cluster. |
| `/connected` | GET/HEAD | Check if the Ray cluster is connected. |
| `/ping` | GET | Health check (returns "pong"). |

**The `/request` endpoint** is the core entry point:

```python
@app.post("/request", dependencies=[Depends(require_ray_connection)])
async def request(
    background_tasks: BackgroundTasks,
    backend_request: BackendRequestModel = Depends(validate_request),
) -> BackendResponseModel:
    response = backend_request.create_response(
        status=ResponseModel.JobStatus.RECEIVED, ...)

    backend_request.request = await backend_request.request  # Await body
    await RedisProvider.async_client.lpush("queue", pickle.dumps(backend_request))

    return response
```

Note that `backend_request.request` starts as a coroutine (from `request.body()`). The `await` materializes the request body, which is then pickled and pushed to Redis. This means the HTTP response is returned after the request is queued, not after it's processed.

**The `/status` endpoint** implements a cache-then-fetch pattern:

1. Check Redis for cached status
2. If not cached, subscribe to `status:event` pub/sub channel
3. Trigger a status request via Redis stream
4. Wait for the Controller to respond (with timeout)
5. Cache the result for `status_cache_freq_s` seconds

### 3.4 WebSocket Communication

NDIF supports two client communication modes:

**Blocking (WebSocket):** The client connects via Socket.IO with a `session_id`. All status updates are pushed to the client in real-time. This is the default mode when `remote=True`.

**Non-blocking (Polling):** The client submits a request and receives a job ID. It then polls `/response/{id}` for updates. Responses are saved to MinIO.

The Socket.IO layer handles several event types:

| Event | Direction | Purpose |
|-------|-----------|---------|
| `blocking_response` | Server → Client | Deliver status updates (QUEUED, RUNNING, COMPLETED, etc.) |
| `stream` | Server → Client | Initialize streaming with room subscription |
| `stream_upload` | Server → Client | Broadcast streaming data to job subscribers |

### 3.5 Configuration

All API configuration is loaded from environment variables via `AppConfig`:

| Variable | Default | Description |
|----------|---------|-------------|
| `NDIF_BROKER_URL` | `redis://localhost:6379` | Redis connection URL |
| `SOCKETIO_MAX_HTTP_BUFFER_SIZE` | `100000000` (100 MB) | Max Socket.IO message size |
| `SOCKETIO_PING_TIMEOUT` | `60` | Socket.IO ping timeout (seconds) |
| `STATUS_REQUEST_TIMEOUT_S` | `60` | Max wait for cluster status |
| `MIN_NNSIGHT_VERSION` | Current installed version | Minimum client nnsight version |
| `MIN_PYTHON_VERSION` | Current Python version | Minimum client Python version |
| `NDIF_DEV_MODE` | `false` | Skip API key validation |

### 3.6 Iterating on this subsystem

The API service is the easiest piece to iterate on because most of it has plain-Python unit tests:

```bash
cd src/ndif/services/api
pytest                                    # unit tests (tests/unit/), no stack needed
pytest tests/unit/test_dependencies.py    # validation rules specifically
```

`tests/unit/conftest.py` sets `DEV_MODE=true` and `NDIF_BROKER_URL` for you, so these run without Redis/Postgres.

For end-to-end coverage of the HTTP path — validation, queueing, Socket.IO delivery — you need a live stack:

```bash
make ta                                  # or make down && make up after non-Docker edits
python scripts/test.py                   # GPT-2 smoke trace against localhost:5001
pytest tests/test_nnsight.py --run-remote
```

**Fast edit loop.** For pure endpoint edits, `make ta` is overkill — the API image only changes when `requirements.in` changes. You can bind-mount `src/ndif/services/api/` into the running `api` container, or just run `gunicorn` natively against the existing compose stack and point it at the same Redis. The Dispatcher re-connects to Ray on restart.

---

## 4. Queue System

### Overview

The queue system is responsible for routing requests from the API endpoint to the Ray cluster. It consists of two main classes:

- **Dispatcher** — A singleton that reads from the Redis queue and routes requests to Processors
- **Processor** — A per-model coordinator that manages deployment lifecycle and request execution

**Key files:**
- `src/ndif/services/api/queue/dispatcher.py`
- `src/ndif/services/api/queue/processor.py`
- `src/ndif/services/api/queue/config.py`
- `src/ndif/services/api/queue/util.py`

### 4.1 The Dispatcher

The Dispatcher is the central coordinator. It runs as a long-lived asyncio event loop with several concurrent tasks:

```
┌─────────────────────────────────────────────────────────┐
│                    Dispatcher                            │
│                                                          │
│  dispatch_worker (main loop)                            │
│    │  1. brpop from Redis "queue" (10s timeout)         │
│    │     then batch rpop up to 32 requests              │
│    │  2. Route each request to a Processor by model_key │
│    │  3. Drain eviction_queue + error_queue             │
│    │                                                    │
│  status_worker (background)                             │
│    │  Respond to cluster status queries                 │
│    │                                                    │
│  queue_state_worker (background)                        │
│    │  Report per-model queue depth for monitoring       │
│    │                                                    │
│  deployment_events_worker (background)                  │
│    │  Handle deploy/evict/kill/env events from Redis    │
│    │  stream "dispatcher:events"                        │
│                                                          │
│  processors: Dict[model_key, Processor]                 │
│  error_queue: shared queue for Processor errors         │
│  eviction_queue: shared queue for Processor evictions   │
└─────────────────────────────────────────────────────────┘
```

The batch-pop is an optimization: `brpop` blocks for up to 10 seconds waiting for the first request, then any additional pending requests are drained non-blocking (`rpop`) up to a cap of 32. This reduces Redis round-trips under load while still allowing periodic eviction/error draining when the queue is idle.

**Startup sequence:**

1. `Dispatcher.start()` creates an instance and calls `asyncio.run(dispatch_worker())`
2. The constructor connects to Redis and Ray (with retry logic)
3. Sets `ray:connected` in Redis to signal the API that Ray is available
4. Spawns `status_worker`, `queue_state_worker`, and `deployment_events_worker` as background asyncio tasks
5. Enters the main `dispatch_worker` loop

**Request routing:**

When a request arrives, the Dispatcher checks if a Processor already exists for the request's `model_key`. If not, it creates one and starts its `processor_worker` as an asyncio task:

```python
def dispatch(self, request: BackendRequestModel):
    if request.model_key not in self.processors:
        processor = Processor(request.model_key, self.eviction_queue, self.error_queue)
        self.processors[request.model_key] = processor
        asyncio.create_task(processor.processor_worker())

    self.processors[request.model_key].enqueue(request)
```

**Error recovery:**

The Dispatcher handles two types of failures:

1. **Connection errors** — If a Processor reports a Ray connection error (or Ray reports disconnected), the Dispatcher purges all Processors, notifies all queued users with an error, and reconnects to Ray.

2. **Execution errors** — Non-connection errors are logged and the affected Processor is reset to `READY` status.

**Events:**

External commands (from the CLI or other tools) are delivered via the Redis stream `dispatcher:events`. The `deployment_events_worker` handles:

| Event | Action |
|-------|--------|
| `QUEUE_STATE_REQUEST` | Return current queue state for monitoring |
| `DEPLOY` | Create a Processor for a newly deployed model |
| `EVICT` | Remove a Processor for an evicted model |
| `KILL_REQUEST` | Cancel a specific request by ID |
| `ENV` | Get Python environment info from Controller |

### 4.2 The Processor

Each Processor manages the lifecycle of a single model deployment. It has its own request queue and transitions through a well-defined state machine:

```
UNINITIALIZED → PROVISIONING → DEPLOYING → READY ↔ BUSY → CANCELLED
```

| State | Description |
|-------|-------------|
| `UNINITIALIZED` | Initial state before any operations |
| `PROVISIONING` | Requesting the Controller to deploy the model |
| `DEPLOYING` | Waiting for the ModelActor to finish loading |
| `READY` | Model is loaded and ready for requests |
| `BUSY` | Currently executing a request (or waiting for error recovery) |
| `CANCELLED` | Terminal state — the Processor is dead |

**The `processor_worker` lifecycle:**

```python
async def processor_worker(self, provision: bool = True):
    self.status = ProcessorStatus.PROVISIONING
    asyncio.create_task(self.reply_worker())

    if provision:
        await self.provision()   # Ask Controller to deploy

    self.status = ProcessorStatus.DEPLOYING
    await self.initialize()       # Wait for ModelActor.__ray_ready__

    self.status = ProcessorStatus.READY

    while self.status != ProcessorStatus.CANCELLED:
        if self.status == ProcessorStatus.BUSY:
            await asyncio.sleep(1)
            continue

        request = await self.queue.get()
        self.status = ProcessorStatus.BUSY
        await self.execute(request)
```

**Provisioning:**

The `provision()` method coordinates with the Controller to ensure the model is deployed:

1. Check if the model is a **dedicated** deployment (scheduled via Google Calendar or CLI)
2. If not dedicated, filter out requests without hotswapping access
3. Ask the Controller to deploy the model
4. Handle evictions — the Controller may evict other models to free GPUs

**Dedicated vs. Hotswapping:**

NDIF has two deployment modes:

- **Dedicated:** Models specified at startup or via the schedule. Available to all users.
- **Hotswapping:** On-demand deployment triggered by a user request. Requires the hotswapping tier on the API key. May evict other non-dedicated models.

**Status updates:**

During provisioning and deployment, a `reply_worker` sends periodic status updates to all queued users (every `processor_reply_freq_s` seconds). This keeps clients informed while they wait.

### 4.3 Request Lifecycle

A request transitions through these statuses as it moves through the system:

```
RECEIVED → QUEUED → DISPATCHED → RUNNING → COMPLETED
                                          → ERROR
                                          → LOG (intermediate)
                                          → STREAM (intermediate)
```

| Status | Where Set | Description |
|--------|-----------|-------------|
| `RECEIVED` | API endpoint | Request validated, pushed to Redis queue |
| `QUEUED` | Processor.enqueue() | Moved into per-model queue with position |
| `DISPATCHED` | Processor.execute() | Sent to ModelActor |
| `RUNNING` | ModelActor.pre() | Execution started |
| `LOG` | ModelActor.log() | Print statement captured |
| `STREAM` | ModelActor.stream_send() | Streaming intermediate data |
| `COMPLETED` | ModelActor.post() | Results uploaded, presigned URL returned |
| `ERROR` | Various | Error at any stage |

### 4.4 Error Handling and Recovery

The system handles errors at multiple levels:

**Processor level:** If `execute()` fails:
- Actor lookup failure ("Failed to look up actor") → Processor is cancelled, model was evicted
- Other errors → Reported to Dispatcher via `error_queue`, Processor stays `BUSY` until Dispatcher clears it

**Dispatcher level:** After every queue poll:
- Drain `eviction_queue` → Remove affected Processors, notify users
- Drain `error_queue` → If connection error detected, purge all Processors and reconnect to Ray

**ModelActor level:** If execution fails:
- CUDA device-side assertion → Actor restarts itself (`ray.kill(no_restart=False)`)
- Other exceptions → Error response sent to client, cleanup runs

**Ray client deadlock workaround:** The Dispatcher applies a patch to Ray's `DataClient._async_send` to prevent a deadlock where a `ClientObjectRef` deletion during an async send causes both operations to compete for the same lock. ⚠️ See §14.5 — the patch is intentional and must not be removed.

### 4.5 Iterating on this subsystem

The Dispatcher and Processor are not testable in isolation in any meaningful way — they only exist in the live path between Redis, Ray, and a ModelActor. "Mocking out Ray" would produce a green test suite that tells you nothing about the thing that usually breaks (race conditions, Ray disconnects, eviction recovery).

Instead:

```bash
make ta                                  # bring up the stack
pytest tests/test_hotswapping.py --run-remote       # deploy / evict / recover
pytest tests/reconnection/ --run-remote             # Ray failure recovery
pytest tests/test_nnsight.py --run-remote           # basic request path
```

**Inspecting live state without shipping a request.** The Dispatcher exposes two side channels:

- The `dispatcher:events` Redis stream accepts commands (`DEPLOY`, `EVICT`, `KILL_REQUEST`, `ENV`, `QUEUE_STATE_REQUEST`). `cli/commands/queue.py` uses this to read per-model queue depth. You can write a quick script that pushes `QUEUE_STATE_REQUEST` and reads the response.
- `ndif status` goes through the same pub/sub path and shows the full cluster state, including Processor status per model.

**Common failure modes when you're changing this code:**
- Processor stuck in `BUSY` → check the `error_queue`. A Processor that hit an error and wasn't cleared will stop consuming requests for that model.
- `/request` hangs at `RECEIVED` → the Dispatcher isn't consuming from Redis. Check the Ray client connection (`ray:connected` Redis key).
- Eviction recovery loops → a Processor keeps re-provisioning. Usually means the Controller can't fit the model and the Processor treats it as a transient error. Check Controller logs for `CANT_ACCOMMODATE`.

---

## 5. Ray Service

### Overview

The Ray service runs as a Ray cluster with a head node hosting the Controller actor and worker nodes hosting ModelActors. The Controller is the brain of the cluster — it tracks resources, manages deployments, and coordinates model lifecycle transitions.

**Key files:**
- `src/ndif/services/ray/start.py` — Ray startup script
- `src/ndif/services/ray/deployments/controller/controller.py` — Controller actor
- `src/ndif/services/ray/deployments/controller/cluster/` — Cluster state management
- `src/ndif/services/ray/deployments/modeling/base.py` — ModelActor

### 5.1 The Controller

The Controller is a Ray actor that runs on the head node. It is defined as a detached actor with unlimited restarts:

```python
@ray.remote(num_cpus=1, num_gpus=0, max_restarts=-1, resources={"head": 1})
class ControllerActor(_ControllerActor):
    pass
```

It is instantiated with configuration from environment variables (see also the consolidated appendix in §15):

| Parameter | Env Variable | Default | Description |
|-----------|-------------|---------|-------------|
| `deployments` | `NDIF_DEPLOYMENTS` | `""` | Pipe-separated model keys to deploy at startup (dedicated) |
| `model_import_path` | — | `ndif.services.ray.deployments.modeling.model:app` | Python path to ModelActor app factory |
| `default_execution_timeout_seconds` | `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | `3600` | Max execution time per request when not overridden |
| `minimum_deployment_time_seconds` | `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` | Min time a non-dedicated model stays deployed before eviction |
| `model_cache_percentage` | `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | Fraction of CPU memory available for warm cache |
| `default_padding_factor` | `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | Multiplicative memory-overhead padding for model-size estimates |
| `default_padding_bias` | `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MB) | Additive memory-overhead padding for model-size estimates |

Node discovery is polled on its own interval:

| Env Variable | Default | Description |
|---|---|---|
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | Interval for `update_nodes()` polling |

**Key responsibilities:**

1. **Cluster state** — Track nodes, GPUs, and memory via periodic `update_nodes()` calls (driven by `check_nodes()` async loop)
2. **Deployment management** — Handle `deploy()` and `evict()` requests from the Dispatcher. `deploy()` accepts a `Dict[MODEL_KEY, DeploymentConfig]` (not a bare list), where `DeploymentConfig` carries `dedicated`, `execution_timeout_seconds`, and similar per-model overrides.
3. **Warm cache management** — `flush_warm_cache(node_ids=None)` drops all WARM models on the specified nodes (or all nodes), transitioning them to COLD and returning the freed memory per node.
4. **Status reporting** — `status()` cross-references Ray's `list_actors()` with the Cluster's view and also reports COLD (downloaded-but-not-deployed) models via `get_downloaded_models()`. `get_deployment(model_key)` returns a single deployment's state.
5. **Environment info** — `env()` reports Python version and installed packages. Used by `ndif env` and `/env` to diagnose client/server version mismatches.
6. **Internal state** — `get_state()` returns the full Controller + Cluster state for debugging.

**Subclass: `SchedulingControllerActor`** (in `deployments/controller/gcal/`) extends `_ControllerActor` to pull dedicated deployments from a Google Calendar. See §5.7.

### 5.2 Cluster Management

The `Cluster` class maintains the authoritative view of the cluster state:

```python
class Cluster:
    nodes: Dict[NODE_ID, Node]     # All GPU-bearing nodes
    evaluator: ModelEvaluator       # Model size cache
```

**Node discovery:**

The `update_nodes()` method polls Ray's `list_nodes()` API every 30 seconds (configurable via `NDIF_CONTROLLER_SYNC_INTERVAL_S`). It:

1. Queries all nodes with `detail=True`
2. Filters to nodes with GPUs (CPU-only nodes are ignored)
3. For new nodes: creates a `Node` with resource tracking
4. For removed nodes: purges all deployments and frees resources

Each `Node` tracks:

```python
@dataclass
class Resources:
    total_gpus: int                    # Total GPU count
    gpu_type: str                      # GPU type identifier
    gpu_memory_bytes: int              # Memory per GPU
    cpu_memory_bytes: int              # CPU memory for caching (total * cache_percentage)
    available_cpu_memory_bytes: int    # Remaining CPU cache capacity
    available_gpus: list[int]          # List of available GPU indices
```

**Deployment algorithm:**

When `deploy()` is called with a list of model keys:

1. **Evaluate** each model's size via `ModelEvaluator` (loads model on meta device, sums parameter sizes, adds 15% padding)
2. **Sort** models by size descending (deploy largest first)
3. For each model, **evaluate every node** as a candidate:

```python
class CandidateLevel(IntEnum):
    DEPLOYED = 0           # Already running on this node
    CACHED_AND_FREE = 1    # Cached in CPU + GPUs available
    FREE = 2               # GPUs available, no cache
    CACHED_AND_FULL = 3    # Cached but need to evict other GPU models
    FULL = 4               # Need to evict GPU models, no cache
    CANT_ACCOMMODATE = 5   # Not enough total GPUs
```

4. **Select the best node** — pick the candidate with the lowest `CandidateLevel`. If multiple nodes tie, choose randomly.
5. **Execute evictions** if needed — models are evicted by fewest GPUs first, respecting the minimum deployment time and dedicated status.

**GPU assignment:**

GPUs are assigned by index from each node's `available_gpus` list. When a model is evicted from GPU, its GPU indices are returned to the node's pool.

### 5.3 Model Evaluation

The `ModelEvaluator` determines how much memory a model needs:

```python
class ModelEvaluator:
    padding_factor: float = 0.15           # multiplicative overhead (15%)
    padding_bias: int = 500 * 1024 * 1024  # additive overhead (500 MB)
    cache: Dict[MODEL_KEY, CacheEntry]     # keyed by model_key; value = CacheEntry

    def __call__(self, model_key, padding_factor=None) -> Union[float, Exception]:
        if model_key not in self.cache:
            meta_model = RemoteableMixin.from_model_key(
                model_key, dispatch=False, torch_dtype=torch.bfloat16,
            )
            param_size = sum(p.nelement() * p.element_size() for p in meta_model._model.parameters())
            buffer_size = sum(b.nelement() * b.element_size() for b in meta_model._model.buffers())
            base = param_size + buffer_size   # CacheEntry stores base, not padded
            self.cache[model_key] = CacheEntry(base, n_params, config, revision)

        entry = self.cache[model_key]
        effective_padding = padding_factor or self.padding_factor
        return math.ceil(entry.base_size_in_bytes
                          + entry.base_size_in_bytes * effective_padding
                          + self.padding_bias)
```

The model is loaded on a meta device (no actual weights) using `RemoteableMixin.from_model_key(..., dispatch=False)`. This gives us the exact parameter counts and dtypes without requiring GPU memory.

**Why an additive bias in addition to a percentage?** Pure percentage padding underestimates overhead for small models (where the CUDA context, workspace memory, and activation buffers dominate) and overestimates for very large models. The 500 MB additive bias captures the roughly-constant per-actor overhead; the 15% factor captures the activation and workspace memory that scales with model size.

**GPU calculation:**

```python
def gpus_required(self, model_size_in_bytes: int) -> int:
    return int(model_size_in_bytes // self.gpu_memory_bytes + 1)
```

This divides the padded model size by per-GPU memory and rounds up.

### 5.4 Deployment Levels

Models exist in one of three states:

| Level | Location | GPU Usage | Availability |
|-------|----------|-----------|-------------|
| **HOT** | GPU | Uses GPUs | Serving requests |
| **WARM** | CPU memory | No GPUs | Fast reload (no disk I/O) |
| **COLD** | Disk | No resources | Slowest reload |

Transitions:

```
COLD → HOT    (create: load from disk, dispatch to GPUs)
HOT → WARM    (cache: move weights to CPU, free GPUs)
WARM → HOT    (from_cache: dispatch weights back to GPUs)
HOT → deleted (delete: kill actor, free all resources)
WARM → deleted (delete: kill actor, free CPU memory)
```

The `Deployment` class represents a model in any of these states:

```python
class Deployment:
    model_key: MODEL_KEY
    deployment_level: DeploymentLevel    # HOT, WARM, or COLD
    gpus: list[int]                      # GPU indices (empty for WARM/COLD)
    size_bytes: int                      # Model size for resource accounting
    dedicated: bool                      # Protected from eviction?
    node_id: str                         # Which node
    deployed: float                      # Timestamp (for minimum deployment time)
```

### 5.5 Deployment Scheduling

The Controller enforces a **minimum deployment time** to prevent thrashing. Non-dedicated models cannot be evicted until `minimum_deployment_time_seconds` has elapsed since deployment.

The eviction algorithm in `Node.evictions()`:

```python
def evictions(self, gpus_required: int, dedicated: bool = False) -> List[MODEL_KEY]:
    deployments = sorted(self.deployments.values(), key=lambda x: len(x.gpus))
    gpus_needed = gpus_required - len(self.resources.available_gpus)
    evictions = []

    for deployment in deployments:
        if deployment.dedicated:
            continue  # Never evict dedicated models
        if not dedicated and deployment hasn't reached minimum time:
            continue  # Respect minimum deployment time
        evictions.append(deployment.model_key)
        gpus_needed -= len(deployment.gpus)
        if gpus_needed <= 0:
            return evictions

    return []  # Can't free enough GPUs
```

Models with fewer GPUs are evicted first (to minimize disruption). Dedicated models are never evicted. The minimum deployment time is only waived when deploying another dedicated model.

### 5.6 The Build/Apply Cycle

When the cluster state changes (deploy or evict), the Controller calls `build()` then `apply()`.

**`build()`** compares the desired state (from `Cluster.nodes`) against the current state (from `Controller.state`) and produces a `DeploymentDelta`:

```python
@dataclass
class DeploymentDelta:
    deployments_to_cache: List[Deployment]     # HOT → WARM
    deployments_from_cache: List[Deployment]    # WARM → HOT
    deployments_to_create: List[Deployment]     # COLD → HOT
    deployments_to_delete: List[Deployment]     # Remove entirely
```

**`apply()`** executes the delta in order:

1. **Delete** actors that need to be removed (`ray.kill(no_restart=True)`)
2. **Cache** models that need to move from GPU to CPU (`actor.to_cache.remote()`) — waits for completion before proceeding, since caching frees GPUs that subsequent operations need
3. **From cache** models that need to move from CPU back to GPU (`actor.from_cache.remote()`) — spawns monitoring tasks to track completion
4. **Create** new models from disk — creates new `ModelActor` actors with specified GPU indices and monitoring tasks

The ordering is critical: deletions and caches must complete before from-cache and creates, because they free the GPU resources needed by the new deployments. ⚠️ See §14 Invariants — this is load-bearing, not a stylistic choice.

### 5.7 Google Calendar Scheduling

NDIF can optionally drive dedicated deployments from a Google Calendar instead of a static `NDIF_DEPLOYMENTS` list or ad-hoc `ndif deploy` invocations. This is the mechanism that runs production NDIF: the schedule lives in a calendar that anyone authorized can edit, and the cluster automatically tracks it.

**Key files:**
- `src/ndif/services/ray/deployments/controller/gcal/controller.py` — `SchedulingControllerActor`
- `src/ndif/services/ray/deployments/controller/gcal/scheduler.py` — `SchedulingActor`

**Architecture:**

```
┌──────────────────────────────────────────────────────────────┐
│                     Ray Head Node                             │
│                                                                │
│   SchedulingControllerActor  (extends _ControllerActor)       │
│          │                                                    │
│          │  ray.get(scheduler.get_schedule.remote())          │
│          │  (used by status() to merge schedule into          │
│          │   cluster state)                                   │
│          ▼                                                    │
│   SchedulingActor                                             │
│          │                                                    │
│          │  every SCHEDULING_CHECK_INTERVAL_S seconds:        │
│          │    1. list events in [now, now+1s]                 │
│          │    2. sanitize descriptions → MODEL_KEYs           │
│          │    3. hash the set; skip if unchanged              │
│          │    4. controller.deploy(                           │
│          │         {key: DeploymentConfig(dedicated=True)})   │
│          │    5. color successful events dark green;          │
│          │       prefix failed events with "ERROR:"           │
│          │       (and email the event creator via Mailgun)    │
│          ▼                                                    │
│   Google Calendar API  ◄──── service account credentials     │
└──────────────────────────────────────────────────────────────┘
```

**How it's enabled.** `SchedulingControllerActor` is a `_ControllerActor` subclass. It replaces the regular `ControllerActor` when `NDIF_CONTROLLER_IMPORT_PATH` points at `ndif.services.ray.deployments.controller.gcal.controller`. Docker compose sets the scheduling env vars (`SCHEDULING_GOOGLE_CALENDAR_ID`, `SCHEDULING_GOOGLE_CREDS_PATH`) and the gcal variant takes over automatically.

**Event format.** Each calendar event's **description** is a `MODEL_KEY`. The event's title is only used for human-readable display. Events are deployed as `dedicated=True`, which means they are protected from eviction (see §5.5) for as long as the event is live. When the event ends and the next poll sees no corresponding event, the model stops being dedicated and can be cached or deleted by the normal eviction path.

**Feedback loop.** The scheduler writes back to the calendar:
- **Successful deployments** get colored `colorId=10` ("Basil" / dark green).
- **Failed deployments** (the calendar references a model the cluster can't accommodate, or that fails to load) get their summary prefixed with `ERROR:` and the event creator receives an email via `MailgunProvider`.

This turns the calendar into both the source of truth and the status dashboard for dedicated deployments.

**Config:**

| Variable | Default | Description |
|---|---|---|
| `SCHEDULING_GOOGLE_CALENDAR_ID` | — | Calendar ID to poll |
| `SCHEDULING_GOOGLE_CREDS_PATH` | — | Path to service account JSON inside the container |
| `SCHEDULING_CHECK_INTERVAL_S` | `10` | Poll interval |
| `SCHEDULING_DELAY_START_S` | `15` | Delay before first poll (lets worker nodes register with the head) |

### 5.8 Iterating on this subsystem

Cluster/Controller logic is the hardest part of NDIF to test in isolation, because "correct" is often about race conditions during state transitions. What you want to exercise is the full `deploy → evict → re-deploy → cache → restore` cycle against a live cluster.

```bash
make ta
pytest tests/test_hotswapping.py --run-remote   # the primary controller validator
```

`test_hotswapping.py` covers the high-risk cases: fractional GPU placement, multi-GPU deployments, eviction while another request is queued, `CANT_ACCOMMODATE` rejection, and HOT ↔ WARM ↔ COLD transitions. If a change to cluster/controller code breaks something real, this is the file that will catch it.

**Inspecting live state:**

```bash
ndif status                       # full cluster + deployment view
ndif deploy <model_key>            # force a deploy via the Controller
ndif evict  <model_key>            # force an evict
ndif queue                         # per-model queue depth
```

Ray's own dashboard (default `http://localhost:8265`) is useful for seeing actor state, log streams, and task graphs — especially when investigating `build()/apply()` bugs or `_monitor_deployment()` failures.

**Tracing.** Every significant Controller action emits an OpenTelemetry span (`controller.deploy`, `controller.build`, `controller.apply`, `controller.monitor_deployment`). If you bring up the Jaeger service (it's in docker compose), you can watch a `deploy()` call and see exactly which delta items were created, which futures were awaited, and which actors became ready. See §10.4.

**Common failure modes when you're changing this code:**
- A new deployment stays `DEPLOYING` forever → the `_monitor_deployment` task hit an exception. Check `ray_dashboard → logs` for the actor and for the Controller.
- A stale deployment isn't evicted → check `minimum_deployment_time_seconds`. `.env.example` sets it to `0` for local dev; if you're testing against a stack that raised it, the eviction will be refused until the timer elapses.
- Fractional GPU placement puts a model on the wrong device → check `_verify_device_placement` in `modeling/base.py`. ⚠️ See §14.1 for why `num_gpus=0` + `torch.cuda.set_device` + `max_memory` is load-bearing.

---

## 6. Model Execution

### Overview

The `ModelActor` is a Ray actor that holds a loaded model and executes user intervention code. It handles the complete lifecycle of a request: deserialization, sandboxed execution, result serialization, and cleanup.

**Key files:**
- `src/ndif/services/ray/deployments/modeling/base.py` — BaseModelDeployment and ModelActor
- `src/ndif/services/ray/nn/backend.py` — RemoteExecutionBackend
- `src/ndif/services/ray/nn/ops.py` — StdoutRedirect

### 6.1 The ModelActor

```python
@ray.remote(num_cpus=2, num_gpus=0, max_restarts=-1)
class ModelActor(BaseModelDeployment):
    pass
```

**Why `num_gpus=0`?** ⚠️ This is deliberate (see §14 Invariants). Ray's GPU scheduler allocates whole GPUs by count, which would prevent NDIF from doing three things it needs:
- Placing a model on *specific* GPU indices rather than letting Ray pick.
- Fractional/shared GPU placement, where two models share one GPU with explicit per-process memory budgets.
- Controlling on which GPU the CUDA context lands. CUDA lazily creates a ~400 MiB context on the first device any CUDA call touches; if that happens to be `cuda:0` on every actor, GPU 0 becomes a bottleneck even for actors that don't "use" it.

Instead, the Controller passes `gpu_mem_bytes_by_id` (a dict of GPU index → byte budget) into the actor, and the actor uses `torch.cuda.set_device(first_gpu)` + `torch.cuda.set_per_process_memory_fraction(...)` + `accelerate.dispatch_model(..., max_memory=...)` to place weights exactly where it wants them. See §14 for the full reasoning.

**Initialization (`__init__`):**

1. Initialize OpenTelemetry tracing (`init_tracing("ndif-ray")`)
2. Connect to MinIO and Socket.IO providers
3. Set `torch.set_default_dtype(bfloat16)`, enable cudnn benchmark, allow TF32 matmul
4. **Before any CUDA call**, call `torch.cuda.set_device(first_gpu_in_budget)` so the context lands on the target GPU
5. Set `torch.cuda.set_per_process_memory_fraction` for each target GPU in `gpu_mem_bytes_by_id`
6. Load model from disk: `RemoteableMixin.from_model_key(..., device_map="auto", max_memory=<built from budget>, dispatch=True, torch_dtype=bfloat16, attn_implementation="eager")` — wrapped in `load_with_cache_deletion_retry` to recover from corrupted HuggingFace caches
7. Extract persistent objects (tokenizer, etc.) via `model._remoteable_persistent_objects()`
8. Wrap `torch.nn.Module` persistent objects in `ProtectedObject` via `protect()`
9. Create the execution `Protector(WHITELISTED_MODULES, builtins=True)` (the narrow whitelist)
10. Disable gradients on the dispatched model (`requires_grad_(False)`)
11. Register `stream_send` / `stream_receive` callbacks on nnsight's `StreamTracer`
12. Initialize `kill_switch` asyncio Event and `_request_count = 0` (used for periodic GC)

### 6.2 Request Deserialization

Before execution, the serialized request must be unpickled. This happens in `pre()`:

```python
def pre(self) -> RequestModel:
    self.respond(status=JobStatus.RUNNING, description="Your job has started running.")

    with Protector(WHITELISTED_MODULES_DESERIALIZATION):
        request = self.request.deserialize(self.persistent_objects)

    return request
```

Deserialization uses a **separate whitelist** (`WHITELISTED_MODULES_DESERIALIZATION`) that includes additional modules needed for unpickling: `pickle`, `cloudpickle`, `copyreg`, `nnsight.schema.request`, `transformers`, etc. These modules are only available during deserialization, not during execution.

The `persistent_objects` dict contains the tokenizer and model reference, which are injected into the deserialized request so the user's code can access them.

### 6.3 Execution Pipeline

The `__call__` method orchestrates the full pipeline:

```python
async def __call__(self, request: BackendRequestModel) -> None:
    try:
        inputs = self.pre()                                    # Deserialize
        job_task = asyncio.create_task(asyncio.to_thread(self.execute, inputs))
        kill_task = asyncio.create_task(self.kill_switch.wait())

        done, pending = await asyncio.wait(
            [job_task, kill_task],
            timeout=self.execution_timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Handle completion, cancellation, or timeout...
        self.post(result)                                      # Serialize results
    except Exception as e:
        self.exception(e)                                      # Error response
    finally:
        self.cleanup()                                         # Free memory
```

The `execute()` method runs in a thread (via `asyncio.to_thread`) so it doesn't block the actor's asyncio event loop:

```python
def execute(self, request: RequestModel) -> Any:
    self.execution_ident = threading.current_thread().ident

    with autocast(device_type="cuda", dtype=torch.get_default_dtype()):
        with StdoutRedirect(self.log):
            result = RemoteExecutionBackend(
                request.interventions, self.execution_protector
            )(request.tracer)

    return result, gpu_mem, execution_time
```

**`RemoteExecutionBackend`** is the bridge between NDIF and NNsight:

```python
class RemoteExecutionBackend(Backend):
    def __call__(self, tracer: Tracer):
        Globals.stack = 0
        Globals.enter()
        try:
            with self.protector:
                saves = tracer.execute(self.fn)
        except Exception as e:
            raise wrap_exception(e, tracer.info) from None
        finally:
            Globals.exit()
        return saves
```

It activates the `Protector` (which patches `__import__` and builtins), then calls `tracer.execute()` with the user's compiled intervention function. The Protector context ensures all user code runs in the sandbox.

### 6.4 Timeout and Cancellation

The actor uses `asyncio.wait` with three competing tasks:

1. **Job task** — The actual execution in a thread
2. **Kill task** — Watches the `kill_switch` asyncio Event
3. **Timeout** — `asyncio.wait`'s `timeout` parameter

If the kill switch fires (from `cancel()`) or the timeout expires, the execution thread is terminated via `kill_thread()`:

```python
if kill_task in done:
    kill_thread(self.execution_ident)
    raise Exception("Your job was cancelled or preempted by the server.")
elif timeout:
    kill_thread(self.execution_ident)
    raise Exception(f"Job took longer than timeout: {self.execution_timeout} seconds")
```

`kill_thread()` uses `ctypes` to raise a `SystemExit` exception in the target thread. This is a last resort — it's not clean, but it prevents runaway execution from blocking the actor.

### 6.5 Cleanup and Memory Management

After every request (success or failure), `cleanup()` runs:

```python
def cleanup(self):
    self.kill_switch.clear()
    self.execution_ident = None
    self.model._model.zero_grad(set_to_none=True)
    self.request = None

    Globals.clear()       # nnsight tracer globals
    clear_set_attrs()     # revert any attribute writes to ProtectedObjects

    self._request_count += 1
    if self._request_count % 5 == 0:
        gc.collect()      # full GC every 5 requests, not every request
```

This ensures:
- Gradients don't accumulate across requests (set to `None`, not zeroed, to avoid allocating a dense zero tensor per param)
- nnsight's per-request tracer state (`Globals`) is reset
- **Attribute writes on `ProtectedObject`s are reverted** via `clear_set_attrs()`. Contrary to the intuition that protected objects are immutable, writes actually *are* allowed during a request — they're tracked and rolled back after cleanup, which is simpler than proving at write-time which assignments are safe (see §7.6).
- The request reference is released so the next request's payload doesn't alias the previous one
- Garbage collection runs every fifth request (not every request — `gc.collect()` is expensive and running it on every request is wasted work when the allocator is already reusing memory)

**Note:** `cleanup()` does *not* call `torch.cuda.empty_cache()`. PyTorch's caching allocator is far better at reusing memory than the OS, so repeatedly releasing and re-acquiring it would only slow subsequent requests down.

For CUDA device-side assertion errors (which corrupt the CUDA context irrecoverably), the actor restarts itself:

```python
if "device-side assert triggered" in str(exception):
    self.restart()  # ray.kill(actor, no_restart=False) — Ray respawns it
```

⚠️ See §14 Invariants for why this is terminal rather than recoverable in-process.

### 6.6 Streaming and Logging

**Print statement capture:**

The `StdoutRedirect` context manager replaces `sys.stdout` during execution. Any `print()` calls in user code are captured and sent as `LOG` status responses:

```python
class StdoutRedirect:
    def write(self, text: str):
        if text.strip():
            self.fn(text)  # Calls ModelActor.log()
```

**Streaming:**

NNsight's `StreamTracer` is used for bidirectional streaming. The ModelActor registers `stream_send` and `stream_receive` callbacks:

- `stream_send(data)` — Wraps data in a `STREAM` response and emits via Socket.IO
- `stream_receive()` — Waits for data from the client via Socket.IO (5-second timeout)

### 6.7 Iterating on this subsystem

Changes to the ModelActor pipeline (`pre` / `execute` / `post` / `cleanup`, stream, log, the init sequence, load/cache/from-cache) are validated end-to-end:

```bash
make ta
pytest tests/test_nnsight.py    --run-remote   # tracing, generation, gradients, sessions, iteration
pytest tests/test_user_code.py  --run-remote   # (de)serialization of user functions, modules, classes
pytest tests/test_hotswapping.py --run-remote  # HOT → WARM → HOT, fractional GPUs, GPU accounting
```

`test_nnsight.py` is the widest net — it's where most regressions surface first. Run it after any change to `modeling/base.py`, `nn/backend.py`, or the streaming/logging path.

**Fast iteration on the load path.** The slowest thing in this subsystem is loading a large model from disk. If you're iterating on `load_from_disk` / `from_cache` / `to_cache`, set `NDIF_DEPLOYMENTS=<small_model>` (e.g. `openai-community/gpt2`) before bringing up the stack — the smallest model takes ~2 seconds to load instead of ~2 minutes.

**Debugging a hung or slow request.** The pipeline is heavily traced (§10.4). A single request's timeline in Jaeger looks like this:

```
model_actor.call
 ├─ model_actor.pre          (deserialize under Protector(DESERIALIZATION))
 ├─ execute (in a thread)    (user code under Protector(EXECUTION) + autocast)
 ├─ model_actor.post         (save to MinIO, emit COMPLETED)
 └─ model_actor.cleanup      (Globals.clear, clear_set_attrs, every-5 GC)
```

If one phase dominates, the span tells you which.

**Common failure modes when you're changing this code:**
- Request completes but no response reaches the client → check `BackendResponseModel.respond()`. The Socket.IO `call` vs `emit` distinction is load-bearing (§8.2).
- GPU memory grows between requests → something in `cleanup()` is leaking. Check that `Globals.clear()` runs and that `self.request = None` is set.
- Cancellation doesn't interrupt a request → ⚠️ see §14.9. `kill_thread` only fires a `SystemExit`; if user code is inside a long-running C extension, the exception is deferred.

---

## 7. Security

### Overview

NDIF executes arbitrary user-submitted Python code on shared GPU infrastructure. The security model must prevent:

1. **Filesystem access** — No reading/writing files
2. **Network access** — No outbound connections
3. **Model modification** — No changing model weights
4. **Sandbox escape** — No accessing internals via `__class__`, `__globals__`, etc.
5. **Resource abuse** — No fork bombs, infinite loops (handled by timeout)

The security system is defense-in-depth: **six layers** that each catch a different class of attack, so that bypassing any one of them still leaves the rest in place. Nothing in this section is best-effort — every layer exists because it closes a specific hole that the other layers can't see.

**Key files** (all under `src/ndif/services/ray/nn/security/`):

| File | Role |
|---|---|
| `whitelist.yaml` | Policy: allowed modules, allowed builtins, blocked submodules, allowed/blocked dunders |
| `whitelist.py` | Loads `whitelist.yaml` into typed Python constants |
| `importer.py` | `Importer`, `SandboxFinder`, `ProtectedModule`, `UnauthorizedModule` |
| `guards.py` | `guarded_getattr`, audit hook, `SAFE_BUILTINS`, `restricted_compile`/`exec` |
| `protector.py` | `Protector` context manager — orchestrates all six layers |
| `protected_objects.py` | `ProtectedObject` — wraps the model/tokenizer (separate from the sandbox) |
| `README.md` | Inline threat model and layer-by-layer reference |

**Dependency graph** (no cycles):

```
whitelist.py
    ↓
importer.py   guards.py
    ↘         ↙
   protector.py
```

When you are changing anything in this directory, read `security/README.md` alongside this section — it is kept current as a policy reference.

### 7.1 The Protector (orchestrator)

`Protector` is a context manager that activates and deactivates all six layers together. It extends nnsight's `Patcher`:

```python
class Protector(Patcher):
    def __init__(self, whitelisted_modules, builtins=False, restrict_compile=True):
        # Layer 1: __import__ replaced with Importer
        self.add(Patch(__builtins__, replacement=self.importer, key="__import__"))

        # Layer 2: SandboxFinder pushed onto sys.meta_path on __enter__
        # Layer 3: cloudpickle.subimport, dynamic_subimport, and
        #          CustomCloudUnpickler.find_class patched (only for the
        #          deserialization whitelist)
        # Layer 4: SAFE_BUILTINS installed; if builtins=True, non-whitelisted
        #          entries are removed from the real __builtins__ dict
        # Layer 5: compile and exec replaced with restricted versions;
        #          guarded getattr/setattr/delattr/hasattr installed in SAFE_BUILTINS
        # Layer 6: audit-hook thread-local flag toggled on __enter__ / off on __exit__

        # Escape hatch: StreamTracer.execute temporarily exits the sandbox so
        # nnsight internals can run unrestricted; re-enters on return.
        self.add(Patch(StreamTracer, replacement=self.escape(StreamTracer.execute), key="execute"))
```

When used as a context manager (`with Protector(...):`), all layers are active for the duration of user code and removed on exit. **Only the user's intervention function runs under the Protector** — nnsight's own internals run unpatched, which is what the `escape()` wrapper on `StreamTracer.execute` is for.

`Protector` is used in two distinct phases with two different whitelists: deserialization (narrow) uses `WHITELISTED_MODULES_DESERIALIZATION` and execution uses `WHITELISTED_MODULES` (§7.8).

### 7.2 Layer 1: Import interception (`importer.py`)

The `Importer` class replaces `__builtins__["__import__"]` while the sandbox is active. Every `import` statement in user code goes through it:

```python
class Importer:
    def __call__(self, name, globals, locals, fromlist, level):
        # Whitelisted module → import normally, then wrap in ProtectedModule
        for module in self.whitelisted_modules:
            if module.check(name):
                result = self.original_import(name, ...)
                return ProtectedModule(result, module)  # immutable, scoped
        # Not whitelisted → lazy placeholder that only errors on use
        return UnauthorizedModule(name)
```

Three key behaviors:

1. **Whitelisted modules** are imported by the real importer, then wrapped in a `ProtectedModule` that is immutable and scopes submodule access (see §7.5). This prevents `torch.os` from leaking through even though `torch` itself is allowed.
2. **Non-whitelisted modules** return an `UnauthorizedModule` — a lazy placeholder that raises `ImportError` only when the user actually *uses* it (attribute access, calling, etc.). **Why lazy?** Many libraries do speculative imports of optional dependencies (`try: import X except ImportError: pass`) that user code never touches. Raising on import would make every whitelisted library that does this unusable.
3. **Blocked submodules** of whitelisted packages are treated as non-whitelisted even though their parent is allowed. Each `whitelist.yaml` entry can carry its own `blocked_submodules` list (e.g., `torch.multiprocessing`, `torch.hub`, `numpy.ctypeslib`).

### 7.3 Layer 2: Meta-path finder (`importer.py` → `SandboxFinder`)

A `SandboxFinder` is pushed onto the front of `sys.meta_path` on `Protector.__enter__` and removed on `__exit__`. This is defense-in-depth against imports that bypass `__import__`.

**Why a separate finder?** Not every import goes through `__builtins__["__import__"]`. For example, `importlib._bootstrap._find_and_load` called from C code consults `sys.meta_path` directly. Without this layer, a carefully-crafted reference inside a whitelisted module's C extension could pull a blocked module into `sys.modules` without the `__import__` patch ever seeing it.

The finder returns `None` for allowed modules (letting normal finders handle them) and a blocking `ModuleSpec` for everything else — one that raises `ImportError` during module execution.

### 7.4 Layer 3: Deserialization hardening (`protector.py`)

User requests arrive as `cloudpickle` payloads. During unpickling, three code paths bypass `__import__` by calling it but then ignoring the return value and reading `sys.modules` directly:

- **`cloudpickle.subimport(name)`** — reconstructs a module object. Patched to check the whitelist before returning.
- **`cloudpickle.dynamic_subimport(name, vars)`** — creates a module from a `vars` dict. Patched with the same check.
- **`pickle.Unpickler.find_class(module, name)`** — reconstructs a class or function reference. Overridden on nnsight's `CustomCloudUnpickler` to walk the whitelist. Uses `pickle._getattribute` to handle dotted names like `Tracer.Info`.

A **separate deserialization whitelist** (`deserialization_modules` in `whitelist.yaml`) temporarily allows pickle/cloudpickle/nnsight internals that the unpickler needs — but user code must not be able to access these. This is why §6.2 uses `Protector(WHITELISTED_MODULES_DESERIALIZATION)` around `pre()` and `Protector(WHITELISTED_MODULES)` around `execute()`. ⚠️ See §14: **never collapse these two whitelists.**

### 7.5 Layer 4: Builtin restriction (`whitelist.py`, `guards.py`)

`SAFE_BUILTINS` is a filtered copy of `__builtins__` containing only the entries named in `whitelist.yaml`. Dangerous builtins are excluded:

- `open` — file I/O
- `__import__` — replaced by the Importer
- `compile`, `exec` — replaced by restricted versions (§7.7)
- `getattr`, `setattr`, `delattr`, `hasattr` — replaced by **guarded versions** that enforce dunder restrictions even outside AST transformation

When the execution `Protector(builtins=True)` is active, non-whitelisted builtins are additionally removed from the real `__builtins__` dict for the duration of user code. This mode is not used during deserialization.

### 7.6 Layer 5: Attribute guards (`guards.py`)

Dangerous dunder attributes (`__class__`, `__globals__`, `__code__`, `__dict__`, `__reduce__`, `__subclasses__`, etc.) are blocked through **two mechanisms**, not one — because Python exposes attribute access via multiple paths:

1. **Guarded builtins.** `getattr`, `setattr`, `delattr`, and `hasattr` in `SAFE_BUILTINS` are replaced with versions that check the attribute name against the dunder blocklist before delegating to the real builtins. This catches `getattr(obj, '__class__')` **without needing AST transformation** — which matters because NDIF does not use RestrictedPython's AST transform (see §7.7).
2. **Guard functions for the AST path** (`_getattr_`, `_getitem_`, `_getiter_`, `_write_`, `_inplacevar_`) are installed by `make_restricted_globals()` and ready to be used if AST transformation is ever re-enabled. They are there as a latent second line of defense.

The allowed/blocked dunder lists are defined in `whitelist.yaml`. Allowed dunders include the safe operations (arithmetic, container, iteration, context managers, comparison); blocked dunders are the classic escape vectors listed above.

**Known limitation:** `obj.__class__` in source-code form (not `getattr(obj, '__class__')`) is **not** caught, because there is no AST transformation. Full dunder blocking would require re-enabling RestrictedPython's AST transform, which currently conflicts with nnsight's internal variable naming. This is a known trade-off documented in `security/README.md`.

### 7.7 Layer 6: Audit hook (`guards.py`)

A `sys.addaudithook` callback blocks dangerous syscall-level operations while the sandbox is active:

- `subprocess.Popen`, `os.system`, `os.exec`, `os.fork`, `os.spawn`, `os.kill`
- `webbrowser.open`, `shutil.rmtree`

⚠️ The audit hook is **permanent per-process** — Python does not let you remove an audit hook once installed. See §14. A `threading.local` flag is toggled by `Protector.__enter__` / `__exit__` so the hook only blocks operations while the sandbox is active. When the Protector temporarily exits for whitelisted operations (e.g. `StreamTracer.execute`), the flag is disabled so internal operations proceed normally.

The hook does **not** block `open` or `import` (which whitelisted modules need during normal execution) — those are handled by layers 1–5 instead.

### 7.8 Module immutability (`ProtectedModule`)

`ProtectedModule` wraps every whitelisted module that the Importer returns to user code, preventing monkey-patching:

```python
class ProtectedModule(ModuleType):
    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if isinstance(attr, ModuleType):
            # Check that submodule is within whitelist scope (handles
            # blocked_submodules and strict entries)
            if not allowed:
                raise AttributeError(f"Module attribute {attr.__name__} is not whitelisted")
            return ProtectedModule(...)  # Recursively protect
        return attr

    def __setattr__(self, name, value):
        raise AttributeError("Cannot modify protected module")
```

**Why recursive?** Without recursion, a user could access `torch.nn.functional` and monkey-patch it even though `torch.nn` is protected — the returned submodule would be a plain `ModuleType`.

The `strict` flag on a `WhitelistedModule` controls submodule access:
- `strict=True`: only the exact module name is allowed (e.g. `operator` but not `operator.attrgetter`).
- `strict=False`: the module and its submodules are allowed (e.g. `torch` includes `torch.nn`, `torch.cuda`, etc.).

A `strict` entry can also carry an `allowed_attributes` list to expose only specific names (e.g. `nnsight.save` without opening the whole `nnsight` namespace).

### 7.9 Protected Objects (`protected_objects.py`)

Separate from the sandbox, `ProtectedObject` wraps loaded models and tokenizers to prevent user mutation. This is what §6.1 step 8 installs.

```python
class ProtectedObject:
    BLOCKED_METHODS = {"to", "cuda", "cpu", "half", "float", "bfloat16", "double", "to_empty"}

    def __getattribute__(self, name):
        if name in BLOCKED_METHODS:
            raise ValueError(f"Attribute `{name}` cannot be accessed")

        value = getattr(PROTECTIONS[id(self)], name)
        if isinstance(value, (torch.Tensor, list, dict)):
            return deepcopy(value)  # Return a copy, not the original
        return value

    def __setattr__(self, name, value):
        SET_ATTRS[id(self)].append((name, getattr(self, name, _SENTINEL)))
        setattr(PROTECTIONS[id(self)], name, value)
```

Key behaviors:

- **Device-movement blocked.** Not just `.to()` — also `.cuda()`, `.cpu()`, `.half()`, `.float()`, `.bfloat16()`, `.double()`, and `.to_empty()`. All of them would let a user shift the model off its assigned GPUs.
- **Tensor / list / dict attributes are deep-copied on read.** This prevents a user from, say, grabbing `model.layers[0].weight`, mutating it in place, and leaving that mutation visible to the next user.
- **Writes are tracked, then reverted.** Contrary to what an "immutable wrapper" would imply, attribute writes are *allowed* during a request — they're recorded in a `threading.local` list and rolled back by `clear_set_attrs()` in `cleanup()` (§6.5). ⚠️ See §14 for why reversion is simpler than refusal.

### 7.10 Restricted compile and exec (`guards.py`)

When user code calls `compile()` or `exec()` within the sandbox, restricted versions replace the builtins:

```python
def restricted_compile(source, filename="<restricted>", mode="exec", ...):
    return _original_compile(source, filename, mode, flags, ...)

def restricted_exec(code, globals=None, locals=None):
    exec_globals = make_restricted_globals(globals)  # inject guards
    _original_exec(code, exec_globals, locals)
```

`make_restricted_globals()` installs the RestrictedPython-style guard functions (`_getattr_`, `_getitem_`, `_getiter_`, `_write_`, `_inplacevar_`) in the exec globals so that AST-transformed code, if present, still routes attribute access through `guarded_getattr`.

⚠️ **Why standard `compile()` instead of RestrictedPython's AST transform?** RestrictedPython rewrites identifiers it considers suspicious, including nnsight's internal `__nnsight_tracer_*` names. With AST transformation on, valid nnsight tracer code fails to execute. Runtime guards (guarded getattr on `SAFE_BUILTINS`, plus layers 1/2/4/6) compensate. See §14.

### 7.11 The whitelist

The whitelist is defined in `whitelist.yaml` and loaded at module import time.

**Allowed modules during execution** (narrow) include `torch`, `numpy`, `collections`, `math`, `time`, `typing`, `einops`, `sympy`, `pandas`, `enum`, `operator` / `_operator`, and `nnsight.intervention.envoy`.

**Additional modules during deserialization only** (broad) include `pickle`, `cloudpickle`, `copyreg`, `nnsight.schema.request`, `nnsight.modeling`, `nnsight.intervention.*`, and `transformers`.

**Notable exclusions:**
- `os`, `sys`, `subprocess` — filesystem and process access
- `socket`, `http`, `urllib` — network access
- `importlib` — dynamic import manipulation
- `ctypes` — arbitrary memory access
- `builtins` is available but filtered through `SAFE_BUILTINS`

**Adding to the whitelist.** To allow a new module:

```yaml
# whitelist.yaml
modules:
  - name: scipy
    strict: false            # allows scipy.linalg, scipy.stats, etc.
    blocked_submodules:
      - scipy.io             # but not scipy.io (file I/O)
```

To expose specific attributes of a strict module only:

```yaml
  - name: nnsight
    strict: true
    allowed_attributes:
      - save
```

⚠️ **Any change to `whitelist.yaml` requires rebuilding the Ray image** (`make ta`) — the file is packaged into the container at build time, not bind-mounted.

### 7.12 Request lifecycle through the sandbox

```
  Client                           Server (ModelActor)
  ──────                           ───────────────────
  import os                        ┌─────────────────────────────────┐
  with model.session(remote=True): │ 1. DESERIALIZATION              │
    os.listdir(".")                │    Protector(DESERIALIZATION)    │
    model.generate(...)            │    ├─ __import__ → Importer     │
         │                         │    ├─ subimport → whitelist     │
    cloudpickle.dumps(session) ──► │    ├─ find_class → whitelist    │
                                   │    └─ meta_path → SandboxFinder │
                                   │    request.deserialize()        │
                                   │    ✗ os ref → ImportError       │
                                   │                                 │
                                   │ 2. EXECUTION                    │
                                   │    Protector(EXECUTION,         │
                                   │             builtins=True)      │
                                   │    ├─ __import__ → Importer     │
                                   │    ├─ builtins → SAFE_BUILTINS  │
                                   │    ├─ getattr → guarded_getattr │
                                   │    ├─ audit hook → enabled      │
                                   │    └─ meta_path → SandboxFinder │
                                   │    tracer.execute(model)        │
                                   │                                 │
                                   │ 3. CLEANUP                      │
                                   │    clear_set_attrs()            │
                                   └─────────────────────────────────┘
```

### 7.13 Iterating on this subsystem

**Read `security/README.md` first.** It's kept in lockstep with the code as a policy reference and is stricter in tone than this section. If the two ever disagree, the README is authoritative for "what the sandbox currently blocks."

**Test suite:**

```bash
make ta                                              # whitelist changes require rebuild!
pytest tests/test_security_guards.py --run-remote    # the primary validator
```

`test_security_guards.py` has two high-level test classes:
- `TestAllowedOperations` — things user code *should* be able to do (import torch, run a trace, use `torch.save` on in-memory tensors, etc.). Any regression here means you've tightened the sandbox too far and broken real workflows.
- `TestBlockedOperations` — things user code must not be able to do (read files, spawn processes, access `__class__`, reach `torch.multiprocessing`, etc.). Any regression here is a **sandbox escape** — treat it as a security bug, not a test failure.

Both classes run against a live stack. Do not "speed up" the loop by mocking out the Protector — it has no semantics that can be meaningfully mocked.

**Editing `whitelist.yaml`:**

```bash
vim src/ndif/services/ray/nn/security/whitelist.yaml
make ta                                              # ⚠️ required — see §14.11
pytest tests/test_security_guards.py --run-remote
```

If you forget `make ta`, the running containers still have the old whitelist baked in and your test results will reflect the old policy, not the new one.

**Adding a new blocked operation.** There are six places a new block could live — pick the right one:

| Attack vector | Layer | File |
|---|---|---|
| User does `import X` where X is dangerous | 1 (Importer) | `importer.py`, `whitelist.yaml::modules` |
| User crafts a pickle payload that reconstructs X via `cloudpickle.subimport` | 3 (deserialization) | `protector.py` |
| User does `getattr(X, '__class__')` | 4/5 (guarded builtins) | `guards.py::guarded_getattr` |
| User calls `subprocess.Popen` or `os.fork` | 6 (audit hook) | `guards.py::_audit_hook` |
| User accesses a dangerous submodule of an allowed package (e.g. `torch.hub`) | 1 (Importer + policy) | `whitelist.yaml::blocked_submodules` |
| User monkey-patches `torch.save = exfil` | N/A — blocked by `ProtectedModule` | `importer.py::ProtectedModule` |

Picking the wrong layer is a common mistake — e.g. adding a check in `guarded_getattr` for a class that users will never touch via `getattr` because they get to it through import. When in doubt, add a failing test in `TestBlockedOperations` first, then figure out which layer's test stops the attack.

**Common failure modes when you're changing this code:**
- `whitelist.yaml` edits don't take effect → you forgot `make ta` (§14.11). The file is baked into the Ray image at build time.
- A new test passes but legitimate user code breaks → you've widened layer 4 or 5 in a way that catches speculative imports inside whitelisted libraries (§14.10 for why `UnauthorizedModule` is lazy).
- `ProtectedObject` writes stop working → you removed or broke `clear_set_attrs()` in `cleanup()` (§14.6). Reads probably still work; the failure shows up as state leaking between users' requests.

---

## 8. Schema and Data Models

### Overview

NDIF uses Pydantic models for request/response serialization. These models handle the full lifecycle of data: receipt from the client, storage in object stores, and delivery back to the client.

**Key files:**
- `src/ndif/common/schema/request.py` — `BackendRequestModel`
- `src/ndif/common/schema/response.py` — `BackendResponseModel`
- `src/ndif/common/schema/result.py` — `BackendResultModel`
- `src/ndif/common/schema/mixins.py` — `ObjectStorageMixin`, `TelemetryMixin`
- `src/ndif/common/types.py` — Type aliases

### 8.1 BackendRequestModel

The `BackendRequestModel` represents a validated request ready for processing:

```python
class BackendRequestModel(ObjectStorageMixin):
    id: REQUEST_ID                                           # UUID
    request: Optional[Union[Coroutine, bytes, ray.ObjectRef]]  # Serialized intervention
    model_key: Optional[MODEL_KEY]                           # NNsight class + HF repo + revision
    session_id: Optional[SESSION_ID]                         # Socket.IO session for blocking mode
    compress: Optional[bool] = True                          # Use zstd compression for results
    api_key: Optional[API_KEY]                               # Authentication key
    hotswapping: Optional[bool] = False                      # Hotswapping tier access
    python_version: Optional[str]                            # Client's Python version
    nnsight_version: Optional[str]                           # Client's NNsight version
    content_length: Optional[int]                            # Request body size
    ip_address: Optional[str]                                # Client IP
    user_agent: Optional[str]                                # Client user agent
```

**The `model_key` format:**

```
nnsight.modeling.language.LanguageModel:{"repo_id": "openai-community/gpt2", "revision": null}
```

This encodes both the NNsight class to use and the HuggingFace repository. The revision `"main"` is normalized to `null` during parsing.

**`from_request()`** constructs the model from a FastAPI `Request` by extracting headers:
- `ndif-api-key` → `api_key`
- `nnsight-model-key` → `model_key`
- `ndif-session_id` → `session_id`
- `nnsight-version` → `nnsight_version`
- `python-version` → `python_version`
- The body becomes an awaitable coroutine stored in `request`

**`deserialize()`** unpickles the request body into an NNsight `RequestModel`:

```python
def deserialize(self, persistent_objects: dict = None) -> RequestModel:
    request = self.request
    if isinstance(self.request, ray.ObjectRef):
        request = ray.get(request)
    return RequestModel.deserialize(request, persistent_objects, self.compress)
```

### 8.2 BackendResponseModel

The `BackendResponseModel` extends NNsight's `ResponseModel` with backend-specific functionality:

```python
class BackendResponseModel(ResponseModel, ObjectStorageMixin, TelemetryMixin):
    callback: Optional[str] = ""     # Email or URL for non-blocking notifications
```

**`respond()`** delivers the response based on the communication mode:

```python
def respond(self):
    if self.blocking:
        # WebSocket delivery
        if COMPLETED or ERROR:
            SioProvider.call("blocking_response", data=(self.session_id, self.pickle()))
        else:
            SioProvider.emit("blocking_response", data=(self.session_id, self.pickle()))
    else:
        # Callback or object store
        if self.callback:
            if is_email(self.callback):
                MailgunProvider.send_email(...)
            else:
                requests.get(f"{self.callback}?status={self.status}&id={self.id}")
        if self.status != JobStatus.LOG:
            self.save()  # Save to MinIO
```

Note the difference between `call()` and `emit()` for Socket.IO:
- `call()` is used for COMPLETED and ERROR (final statuses) — it waits for acknowledgment
- `emit()` is used for intermediate statuses (QUEUED, RUNNING, LOG) — fire and forget

### 8.3 BackendResultModel

The `BackendResultModel` stores the actual computation results:

```python
class BackendResultModel(ObjectStorageMixin):
    _folder_name: ClassVar[str] = "results"
    _file_extension: ClassVar[str] = "pt"     # Stored as PyTorch tensors
```

Results are serialized using `torch.save()` with a custom `TensorStoragePickler` that moves GPU tensors to CPU before pickling. This prevents serialization errors from GPU tensors.

Optional zstd compression (level 6) is applied when `compress=True` (the default).

### 8.4 Object Storage Mixin

The `ObjectStorageMixin` provides S3-compatible storage operations:

```python
class ObjectStorageMixin(BaseModel):
    _folder_name: ClassVar[str]     # Bucket prefix (e.g., "requests", "results")
    _file_extension: ClassVar[str]  # Determines serialization format

    def save(self, compress=False) -> Self    # Upload to MinIO
    def load(cls, id) -> Self                 # Download from MinIO
    def url(self) -> str                      # Generate presigned URL (2-hour expiry)
    def delete(cls, id) -> None               # Remove from MinIO
```

Object keys follow the pattern: `{folder_name}/{id}.{extension}`

For `pt` files, a custom `TensorStoragePickler` is used:

```python
class TensorStoragePickler(pickle.Pickler):
    def reducer_override(self, obj):
        if torch.is_tensor(obj) and obj.device.type != "cpu":
            return obj.detach().to("cpu").__reduce_ex__(pickle.HIGHEST_PROTOCOL)
        return NotImplemented
```

### 8.5 Response Delivery

The complete response delivery flow:

1. **ModelActor.post()** — After execution, saves the result to MinIO and sends a COMPLETED response with the presigned URL and result size
2. **BackendResponseModel.respond()** — Routes the response via WebSocket (blocking) or MinIO + callback (non-blocking)
3. **NNsight client** — Downloads the result from the presigned URL and deserializes it

---

## 9. Infrastructure and Providers

### Overview

NDIF uses a provider pattern for external service connections. Each provider is a static class that manages its own connection lifecycle and exposes both sync and async interfaces where needed.

**Key files:**
- `src/ndif/common/providers/redis.py` — Redis connections
- `src/ndif/common/providers/objectstore.py` — MinIO/S3 connections
- `src/ndif/common/providers/socketio.py` — Socket.IO client
- `src/ndif/common/providers/mailgun.py` — Email notifications

### 9.1 Redis

Redis serves three roles in NDIF:

1. **Request queue** — The main `queue` list where API pushes and Dispatcher pops
2. **Pub/Sub** — Status events (`status:event`), connection state (`ray:connected`)
3. **Streams** — Dispatcher events (`dispatcher:events`, `status:trigger`)
4. **Caching** — Status cache, env cache
5. **Socket.IO backend** — Cross-process WebSocket message routing

The `RedisProvider` exposes both sync and async clients:

```python
class RedisProvider:
    sync_client: redis.Redis
    async_client: redis.asyncio.Redis
```

Sync is used in the Dispatcher's connect method (which blocks). Async is used in all asyncio code.

### 9.2 Object Store (MinIO/S3)

MinIO provides S3-compatible object storage for:

- **Results** — `results/{id}.pt` — PyTorch-serialized computation results
- **Responses** — `responses/{id}.json` — Status responses for non-blocking polling

The `ObjectStoreProvider` wraps a boto3 S3 client and auto-creates buckets on first use.

### 9.3 Socket.IO

The `SioProvider` manages a Socket.IO client used by the Dispatcher and ModelActors to send real-time updates to connected clients. It connects to the same Redis-backed Socket.IO manager as the API's server-side manager, enabling cross-process communication.

### 9.4 PostgreSQL (auth / API keys)

PostgreSQL stores API keys and tier assignments. The `AccountsDB` class provides:

```python
class AccountsDB:
    def api_key_exists(self, key_id: API_KEY) -> bool
    def key_has_hotswapping_access(self, key_id: API_KEY) -> bool
```

These are called from `src/ndif/services/api/dependencies.py::validate_request` to authenticate incoming requests and decide whether the key is allowed to hotswap (§3.2). The connection pool is managed by `src/ndif/common/providers/postgres.py`, which reads `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_MIN_CONNECTIONS`, and `POSTGRES_MAX_CONNECTIONS`.

**Dev-mode bypass.** When `NDIF_DEV_MODE=true` (the default in `.env.example`), `db.py` short-circuits every authentication call to return `True` and Postgres is not touched at all. This is what makes `scripts/test.py` and the remote pytest suite work against a fresh local stack without seeding any keys.

**Dev-mode schema (`docker/postgres/init.sql`).** The Docker stack ships a minimal Postgres schema that is loaded on first container start via a docker-entrypoint init script. The schema is:

```
keys          (key_id UUID PRIMARY KEY, created_at TIMESTAMPTZ)
tiers         (tier_id UUID PRIMARY KEY, name UNIQUE)
key_tier_assignments  (key_id FK, tier_id FK, PRIMARY KEY (key_id, tier_id))
```

It inserts a single **hotswapping** tier and a known test key (`12345678-1234-5678-1234-567812345678`) already granted hotswapping access. This is the same key hardcoded in `scripts/test.py`, so the smoke test works regardless of whether you have `NDIF_DEV_MODE=true` or are exercising the real auth path through Postgres.

**Production schema.** The production deployment uses a much richer schema (user accounts, foreign keys, audit columns) that lives outside this repo. The schema here is deliberately minimal — enough to exercise the same code path in development without having to mock it.

---

## 10. Telemetry and Monitoring

### Overview

NDIF collects metrics at multiple points in the request lifecycle. These are exported to InfluxDB (via custom metrics) and Prometheus (via FastAPI instrumentation) and visualized in Grafana.

### 10.1 Metrics

| Metric | Class | What It Measures |
|--------|-------|-----------------|
| `GPUMemMetric` | Per-request | Peak GPU memory above model baseline |
| `ModelLoadTimeMetric` | Per-model | Time to load from disk or cache |
| `NetworkStatusMetric` | Per-request | Request metadata (size, IP, etc.) |
| `ExecutionTimeMetric` | Per-request | Wall clock execution time |
| `RequestResponseSizeMetric` | Per-request | Result payload size in bytes |
| `RequestStatusTimeMetric` | Per-status | Time spent in each status |

Metrics are written to InfluxDB asynchronously using the InfluxDB Python client.

### 10.2 Logging

NDIF uses Python's `logging` module with structured log messages. In Docker deployments, logs are shipped to Grafana Loki for centralized viewing.

Log sources:
- **API service** — Request validation, queue operations
- **Dispatcher** — Request routing, error recovery
- **Controller** — Deployment decisions, cluster state changes
- **ModelActor** — Execution lifecycle, GPU memory

### 10.3 Grafana Dashboards

Pre-configured dashboards (in `telemetry/grafana/dashboards/`) provide:
- Request throughput and latency
- GPU memory usage per model
- Model load times
- Queue depth and processing rates
- Error rates by model

### 10.4 Distributed Tracing (OpenTelemetry → Jaeger)

In addition to metrics, NDIF emits **OpenTelemetry traces** that span the full request lifecycle across the API and Ray services. This is what lets you look at a single request in Jaeger and see every step: validation → queue push → dispatch → provisioning → actor load → `pre()` → `execute()` → `post()`.

**Key files:**
- `src/ndif/common/tracing/` — shared setup, used by the Ray service and Dispatcher
- `src/ndif/services/api/tracing/` — API-specific wiring (FastAPI instrumentation + request attributes)
- Both directories expose `init_tracing(service_name)`, `trace_span(...)`, `set_request_attributes(span, request)`, and `TracingContext` for propagation

**Setup.** Each service calls `init_tracing("ndif-<service>")` once at startup. This creates a `TracerProvider`, installs a `BatchSpanProcessor`, and — if `OTEL_EXPORTER_OTLP_ENDPOINT` is set — attaches an OTLP gRPC exporter pointing at Jaeger. If the env var is not set, tracing is a no-op (the spans are still created in memory but no exporter consumes them), so unit tests and ad-hoc runs work without Jaeger.

**Propagation across service boundaries.** Because the Dispatcher runs in the API process and talks to Ray actors via the Ray client, a request's trace context has to be serialized across the wire. The convention is:

1. The API endpoint creates a root span and captures the current context via `TracingContext.inject()` into a dict.
2. That dict (`trace_context`) is stored on `BackendRequestModel` and pickled into Redis.
3. The Dispatcher → Processor → ModelActor chain each pull it back out via `TracingContext.extract(trace_context)` and use it as the parent context for their own spans.

This gives Jaeger a single continuous trace even though the request crossed three processes. ModelActor initialization (`model_actor.init`, `model_actor.load`) and per-request spans (`model_actor.pre`, `model_actor.call`, `model_actor.post`, `model_actor.cleanup`) all hang off the same root.

**Named spans you can grep for** (useful when debugging a specific subsystem):

| Span name | Emitted by |
|---|---|
| `controller.deploy`, `controller.evict`, `controller.build`, `controller.apply` | Controller state transitions |
| `controller.monitor_deployment` | Async task waiting on actor readiness |
| `model_actor.init`, `model_actor.load` | Initial load + cache/restore |
| `model_actor.call`, `model_actor.pre`, `model_actor.post`, `model_actor.cleanup` | Per-request pipeline |
| `model_actor.to_cache`, `model_actor.from_cache` | HOT↔WARM transitions |

**Config:**

| Variable | Default | Description |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | OTLP gRPC endpoint (e.g. `http://jaeger:4317`). Unset = no-op. |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5` | Exporter timeout in seconds |

Docker compose ships a Jaeger service at `jaeger:4317` for OTLP gRPC and `jaeger:16686` for the UI.

---

## 11. CLI

### Overview

The NDIF CLI (`ndif`) manages service lifecycles, monitoring, and operational tasks. It uses Click for command-line parsing and manages persistent sessions that track which services are running.

**Key files:**
- `cli/cli.py` — Main entry point
- `cli/commands/` — Individual command implementations
- `cli/lib/session.py` — Session management
- `cli/config.py` — Configuration defaults

### 11.1 Session Management

A **session** represents the lifetime of NDIF services from `ndif start` to `ndif stop`. Sessions are stored in `~/.ndif/` (configurable via `NDIF_SESSION_ROOT`):

```
~/.ndif/
├── current -> session_20250121_143052/    # Symlink to active session
└── session_20250121_143052/
    ├── config.json                        # SessionConfig
    └── logs/
        ├── api/output.log
        ├── ray/output.log
        ├── broker/
        └── object-store/
```

The `SessionConfig` dataclass captures the full configuration at the time of `ndif start`:

```python
@dataclass
class SessionConfig:
    session_id: str
    broker_url: str
    object_store_url: str
    api_url: str
    ray_address: str
    ray_temp_dir: str
    ray_head_port: int
    # ... all port/config values
    node_type: str = "head"          # "head" or "worker"
    services: dict = field(...)       # Service states
```

All configuration values are loaded from environment variables at session creation time. This means the session captures the exact configuration used, enabling reproducibility.

### 11.2 Commands

| Command | Description |
|---------|-------------|
| `ndif start [service]` | Start services (all, api, ray, broker, object-store) |
| `ndif start --worker` | Start as a Ray worker node |
| `ndif stop` | Stop all running services |
| `ndif restart [service]` | Restart services |
| `ndif status` | Show cluster status (models, resources) |
| `ndif deploy <model_key>` | Deploy a model to the cluster |
| `ndif evict <model_key>` | Evict a model from the cluster |
| `ndif logs <service>` | View service logs |
| `ndif queue` | Show queue state (pending requests per model) |
| `ndif kill <request_id>` | Cancel a specific request |
| `ndif info` | Show current session information |
| `ndif env` | Show Ray cluster Python environment |
| `ndif export` | Export the current session's config (for reproducing a setup) |

**`ndif start` flow:**

1. Print NDIF logo
2. Check for existing session
3. Build config from environment
4. Determine which services need starting
5. Run pre-flight checks (port availability, dependency reachability)
6. If all checks pass, create/reuse session
7. Start services in order: broker → object-store → ray → api
8. If any service fails, rollback (stop all started services, clean up session)

**Pre-flight checks** include:
- Port availability (is the port already in use?)
- Dependency reachability (can we connect to Redis? MinIO?)
- Ray temp directory (does it exist and is it writable?)

### 11.3 Worker Nodes

NDIF supports multi-machine Ray clusters. Worker nodes contribute GPUs to the cluster without running the API or queue services.

```bash
# On the head node:
ndif start

# On worker nodes (set NDIF_RAY_ADDRESS to point to head):
export NDIF_RAY_ADDRESS=ray://head-ip:10001
ndif start --worker
```

Worker sessions have `node_type="worker"` and only track the `ray-worker` service. The Controller automatically discovers new nodes via `list_nodes()` and includes their GPUs in scheduling decisions.

---

## 12. Deployment

### Overview

NDIF can be deployed in two ways: via Docker Compose (production) or natively via the CLI (development).

### 12.1 Docker

The Docker setup uses a multi-purpose `Dockerfile` with a `NAME` build arg:

```dockerfile
FROM astral/uv:python3.12-trixie-slim

ARG NAME
COPY . /ndif
WORKDIR /ndif
RUN uv pip install --system -r src/ndif/services/${NAME}/requirements.in && \
    pip install . --no-deps

CMD ndif start "${NAME}"
```

This builds two images from the same Dockerfile:
- `api:latest` — The API service
- `ray:latest` — The Ray head node

The `docker-compose.yml` orchestrates seven services:

| Service | Image | Purpose |
|---------|-------|---------|
| `message_broker` | `redis` | Request queue, pub/sub, Socket.IO backend |
| `minio` | `minio/minio` | Object storage for results |
| `ray` | `ray:latest` | Ray head + Controller + ModelActors |
| `api` | `api:latest` | FastAPI + Dispatcher |
| `prometheus` | `prom/prometheus` | Metrics collection |
| `influxdb` | `influxdb` | Time-series metrics storage |
| `grafana` | `grafana/grafana` | Monitoring dashboards |

**Build and run:**

```bash
make build   # Build api and ray images
make up      # Start all containers
make down    # Stop all containers
make ta      # Full rebuild: down + build + up
```

### 12.2 Native (CLI)

For development, the CLI manages services directly:

```bash
ndif start       # Start Redis, MinIO, Ray head, API server
ndif status      # Check what's running
ndif logs api    # View API logs
ndif stop        # Stop everything
```

The CLI starts Redis and MinIO via their respective binaries, Ray via `ray start --head`, and the API via Gunicorn. All processes are tracked in the session's service state.

### 12.3 Configuration Reference

All configuration is via environment variables. The defaults shown below are what the code uses when no environment variable is set; `.env.example` ships with its own overrides tuned for local development (e.g. it sets `NDIF_DEV_MODE=true` and `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS=0`). **The consolidated reference with every variable grouped by subsystem lives in §15.** The tables here are a quick reference for the most common knobs.

**Core Services:**

| Variable | Code Default | Description |
|----------|---------|-------------|
| `NDIF_BROKER_URL` | `redis://localhost:6379` | Redis connection URL |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:27018` | MinIO S3 endpoint |
| `NDIF_API_PORT` | `5001` (compose) | API server port |
| `NDIF_API_WORKERS` | `1` | Gunicorn worker count |
| `NDIF_DEV_MODE` | `false` (code), `true` (`.env.example`) | Skip API key validation |

**Ray Cluster:**

| Variable | Code Default | Description |
|----------|---------|-------------|
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | Ray client connection address |
| `NDIF_RAY_HEAD_PORT` | `6385` (compose) | Ray head node port |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | Ray dashboard port |
| `NDIF_RAY_SERVE_PORT` | `8262` | Ray Serve / metrics port |
| `NDIF_RAY_TEMP_DIR` | `/tmp/ray` | Ray temporary directory |

**Controller:**

| Variable | Code Default | Description |
|----------|---------|-------------|
| `NDIF_CONTROLLER_IMPORT_PATH` | `ndif.services.ray.deployments.controller.controller` | Python path to Controller module |
| `NDIF_DEPLOYMENTS` | `""` | Pipe-separated model keys to deploy at startup (dedicated) |
| `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | `3600` | Max execution time per request when not overridden per-deployment |
| `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` (code), `0` (`.env.example`) | Min time before a non-dedicated model can be evicted |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | Fraction of CPU memory for warm cache |
| `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | Multiplicative memory-overhead padding |
| `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MB) | Additive memory-overhead padding |
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | Interval for node discovery polling |

**Queue:**

| Variable | Code Default | Description |
|----------|---------|-------------|
| `COORDINATOR_STATUS_CACHE_FREQ_S` | `120` | How long to cache cluster status in Redis |
| `COORDINATOR_PROCESSOR_REPLY_FREQ_S` | `3` | Status update frequency for queued users |

**Database (dev-mode bypassed when `NDIF_DEV_MODE=true`):**

| Variable | Code Default | Description |
|----------|---------|-------------|
| `POSTGRES_HOST` | `localhost` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | PostgreSQL port |
| `POSTGRES_DB` | `ndif` (code), `keys` (compose) | Database name |
| `POSTGRES_USER` | `""` | Database user |
| `POSTGRES_PASSWORD` | `""` | Database password |
| `POSTGRES_MIN_CONNECTIONS` | `1` | Connection pool minimum |
| `POSTGRES_MAX_CONNECTIONS` | `10` | Connection pool maximum |

---

## 13. Monitor Service

### Overview

`src/ndif/services/monitor/` is a **standalone** uptime monitor that is not part of the main NDIF stack. It lives in the repo so it can be maintained alongside the API/Ray services, but it **does not run inside docker compose** — it runs on a separate host via a cron job and is deployed with its own `run.sh` script. The goal is simple: answer "is NDIF up right now?" continuously and without depending on any of the components it is monitoring.

**Why separate?** If the monitor ran inside the same docker stack, a compose failure would take the monitor down with it — exactly when you need it most. Pulling it out of the stack means "NDIF is down" and "the monitor can't see NDIF" are two independent signals.

**Key files:**
- `src/ndif/services/monitor/run.sh` — setup + cron deploy script
- `src/ndif/services/monitor/jobs/monitor.py` — single unified monitor script
- `src/ndif/services/monitor/jobs/util.py` — config loading, Discord webhook, log rotation
- `src/ndif/services/monitor/dashboard/dashboard.py` — Flask backend serving uptime/latency charts
- `src/ndif/services/monitor/dashboard/dashboard.html` — frontend (Chart.js, dark/light toggle)
- `src/ndif/services/monitor/config.example.json` — example Discord webhook + message templates
- `src/ndif/services/monitor/README.md` — operator guide

### 13.1 What it does

A single script (`jobs/monitor.py`) runs every 10 minutes via cron. Each invocation:

1. **Checks `/connected`** on the NDIF API — is it reachable and returning 200?
2. **Every 2 hours** (or every run while recovering from downtime), fetches `/status`, enumerates HOT models, and runs a real nnsight trace against each one. This catches cases where the API is up but inference is broken (e.g. a model fell off the wire).
3. **Sends Discord notifications** on *state transitions*: going down, still down, and coming back up. Failed model traces get a separate warning. "Still down" is rate-limited so a prolonged outage does not spam the channel.

NDIF is considered **up** only after a full clean run — connected check + status fetch + every HOT-model trace — passes. Any partial failure leaves it in the **down** state.

### 13.2 Deployment (outside the stack)

```bash
export NDIF_API_KEY=your_key
export INSTALL_DIR=~/ndif_monitor   # optional, default is ~/ndif_monitor

# Inside the repo:
cd src/ndif/services/monitor
./run.sh
```

`run.sh` is idempotent. It:
- Creates a `monitor` conda env (Python 3.12) if it does not exist
- Installs the `ndif` package into that env — this pulls monitor's deps (`nnsight`, `requests`, `flask`) via the project's aggregated requirements
- Cron invokes `python -m ndif.services.monitor.jobs.monitor` from the conda env's site-packages snapshot — repo changes do not affect a running deployment until you re-run `run.sh`
- Creates `$INSTALL_DIR/config.json` from `config.example.json` if missing
- Installs or updates a cron job (schedule from `MONITOR_CRON`, default `*/10 * * * *`)

Re-run `run.sh` after making changes to deploy them. The decoupling between "source in repo" and "deployed in install dir" is intentional: you can edit the monitor without worrying about a mid-deploy crashing the running cron job.

**Install directory layout:**

```
$INSTALL_DIR/
  config.json         # Discord webhook + message templates
  jobs/
    monitor.py        # Main cron script
    util.py           # Shared utilities
  dashboard/
    dashboard.py      # Flask server
    dashboard.html    # Frontend
  logs/
    .state.json       # up/down state between runs
    connected_YYYYMMDD.log
    models_YYYYMMDD.log
```

### 13.3 Dashboard

The dashboard is a Flask server that reads the log files in `$INSTALL_DIR/logs/` and renders:
- A connectivity calendar (click a day to see a 24-hour timeline of 10-minute check slots)
- Average and per-model latency charts
- Per-model uptime timelines (30 days, 2-hour resolution)

Start it with:

```bash
python $INSTALL_DIR/dashboard/dashboard.py --log-dir $INSTALL_DIR/logs
# Serves on port 8080 by default; --host and --port to override
```

It refreshes every 5 minutes. There is no login — expose it carefully.

### 13.4 Configuration

| Variable | Default | Description |
|---|---|---|
| `INSTALL_DIR` | `~/ndif_monitor` | Where source, logs, and config live |
| `NDIF_API_KEY` | — | API key for nnsight remote traces |
| `MONITOR_CRON` | `*/10 * * * *` | Cron schedule expression |

`$INSTALL_DIR/config.json`:

```json
{
    "discord_webhook": "https://discord.com/api/webhooks/...",
    "discord_role_id": "1252363632805806240",
    "messages": {
        "down": "...",
        "still_down": "...",
        "back_up": "...",
        "models_failed": "..."
    }
}
```

Message templates support: `{reason}`, `{timestamp}`, `{down_since}`, `{mention}`, `{failed_count}`, `{total}`, `{model_list}`.

---

## 14. Invariants

Several things in this codebase look over-complicated, redundant, or outright wrong at first glance. They are not. Each of the items in this section was the second (or third) attempt and exists to dodge a specific failure mode. Before you "simplify" any of them, read the entry.

Entries are cross-referenced from the section where the mechanism is first described. Inline ⚠️ markers throughout the document point back here.

### 14.1 `ModelActor` is declared with `num_gpus=0`

*Location:* `src/ndif/services/ray/deployments/modeling/base.py` — `@ray.remote(num_cpus=2, num_gpus=0, ...)`. See §6.1.

Ray's GPU scheduler allocates **whole GPUs by count**. If `ModelActor` were declared with `num_gpus=N`, three things NDIF needs would break:

1. **Specific GPU placement.** Ray would pick *which* GPUs each actor gets. NDIF's scheduler needs to pin actors to specific indices so it can track per-GPU memory and avoid fragmentation across nodes.
2. **Fractional / shared GPU placement.** Two small models sharing one GPU with explicit per-process memory budgets is a common pattern (e.g. two 10 GB models on one 40 GB card). Ray's scheduler would refuse to co-schedule them unless each requested `num_gpus=0.5`, which would still not give NDIF control over the split.
3. **CUDA-context placement.** CUDA lazily creates a ~400 MiB context on the first device any CUDA call touches. If every ModelActor accidentally touches `cuda:0` first (e.g. the default device at import time), GPU 0 accumulates one context per actor across the whole node, even for actors whose weights live elsewhere. `BaseModelDeployment.__init__` explicitly calls `torch.cuda.set_device(first_gpu_in_budget)` **before any CUDA call** specifically to stop this. That call only works because the Controller — not Ray — decides which GPU is "first."

Instead of `num_gpus=N`, the Controller passes `gpu_mem_bytes_by_id` (a dict of GPU index → byte budget) into each actor, and the actor uses:

```python
torch.cuda.set_device(first_gpu)
for gpu_id, mem_bytes in gpu_mem_bytes_by_id.items():
    torch.cuda.set_per_process_memory_fraction(mem_bytes / total_mem, gpu_id)
...
model = RemoteableMixin.from_model_key(..., max_memory=max_memory_dict)
# accelerate.dispatch_model respects max_memory to place each layer
```

**Do not:** change `num_gpus` to match the declared budget, collapse the explicit `set_device` call, or remove the `set_per_process_memory_fraction` loop. Each of those is load-bearing for either correctness (wrong placement) or efficiency (wasted VRAM).

### 14.2 Two whitelists, not one

*Location:* `src/ndif/services/ray/nn/security/whitelist.yaml` + `whitelist.py`. See §7.4, §7.11.

The sandbox has a **narrow execution whitelist** (`WHITELISTED_MODULES`) and a **broader deserialization whitelist** (`WHITELISTED_MODULES_DESERIALIZATION`). The broader one adds `pickle`, `cloudpickle`, `copyreg`, `nnsight.schema.request`, `nnsight.modeling`, `nnsight.intervention.*`, and `transformers`.

Both are real; neither is dead code. `ModelActor.pre()` activates the deserialization `Protector`, deserializes the request, then exits. `ModelActor.execute()` activates the execution `Protector` separately. User code only ever runs under the narrow one.

**Why not a single wider whitelist that covers both?** Because cloudpickle can reconstruct arbitrary objects from whitelisted modules during unpickling. If `transformers` were in the execution whitelist, a user could pickle a payload that reconstructs a `transformers.AutoTokenizer` whose internals bypass the Importer entirely. The narrow execution whitelist denies user code the vocabulary to name the escape vectors that the unpickler needs to do its job.

**Why not a single narrower whitelist that drops the deserialization extras?** Because `RequestModel.deserialize` would fail — `pickle.Unpickler.find_class` needs to import the actual classes to reconstruct them.

**Do not:** merge the two whitelists, add execution-only modules to the deserialization set (or vice versa), or try to "simplify" by replacing the two `Protector(...)` contexts with one.

### 14.3 `compile()` is stock Python, not RestrictedPython's AST transform

*Location:* `src/ndif/services/ray/nn/security/guards.py` — `restricted_compile`. See §7.10.

RestrictedPython's AST transformation rewrites identifiers it considers "suspicious." That includes nnsight's internal `__nnsight_tracer_*` names. With AST transformation on, valid nnsight tracer code fails to execute because RestrictedPython strips exactly the hooks nnsight relies on.

Runtime guards compensate:

- Layer 1 (`Importer`) catches imports at statement time.
- Layer 2 (`SandboxFinder`) catches imports at the C level.
- Layer 4 (guarded `getattr`/`setattr` in `SAFE_BUILTINS`) catches `getattr(obj, '__class__')`.
- Layer 6 (`sys.addaudithook`) catches syscall-level escapes like `subprocess.Popen` or `os.fork`.

**Known trade-off:** `obj.__class__` in source form (not `getattr(obj, '__class__')`) is not caught, because there is no AST rewrite. This is documented in `security/README.md` under "Known Limitations." Accepting this limit is the whole reason for the six-layer design — no single layer is sufficient, and the AST layer is the one that doesn't work in our environment.

**Do not:** re-enable RestrictedPython's AST transform without a separate, verified path for nnsight's tracer names. If you do need AST-level dunder blocking, the guard functions (`_getattr_`, `_write_`, etc.) are already present in `make_restricted_globals()` — they're wired up and waiting, but turning them on without the tracer-name work will break every real request.

### 14.4 `build()`/`apply()` ordering is delete → cache → from_cache → create

*Location:* `src/ndif/services/ray/deployments/controller/controller.py` — `_ControllerActor.apply()`. See §5.6.

The apply step executes the `DeploymentDelta` in a specific order:

1. **Delete** (`ray.kill(no_restart=True)`) — frees GPUs that the deleted actors held.
2. **Cache** (`actor.to_cache.remote()` + `ray.get(cache_future)`) — moves HOT models to CPU. **This is synchronous** — the Controller waits for every cache to complete before moving on.
3. **From cache** (`actor.from_cache.remote()` + async monitoring task) — moves WARM models back to GPU.
4. **Create** (new `ModelActor` + async monitoring task) — loads fresh models from disk.

**Why this order?** Steps 3 and 4 allocate GPU memory; steps 1 and 2 free it. If step 4 happened before step 2, the new actors would try to load onto GPUs that still have the old weights, get CUDA OOM, and fail. If step 2 were async, step 3 could race step 2 on the same GPU.

**Why step 2 is sync but steps 3 and 4 are async.** Step 2 has to complete before anything else can proceed — there is no way to overlap it with the rest without races. But steps 3 and 4 only need to *start* before `apply()` returns; their completion is tracked by `_monitor_deployment()` async tasks that clean up the deployment from state if it fails to become ready. This keeps the Controller's main loop responsive while long loads finish in the background.

**Do not:** reorder these steps, make step 2 async, or make step 4 synchronous (it would block the Controller for the duration of every model load).

### 14.5 The Ray client deadlock patch

*Location:* `src/ndif/services/api/queue/util.py` — `patch()`. See §4.4.

The Dispatcher applies a monkey patch to `ray.util.client.dataclient.DataClient._async_send` that disables `ClientObjectRef.__del__` for the duration of each async send.

**Why:** Ray's data client has a lock-ordering bug where `ClientObjectRef.__del__` (which runs in a finalizer thread) and `DataClient._async_send` (which runs in the main thread) both need the same lock. If a finalizer fires while an async send is in flight, they deadlock. Disabling the finalizer for the duration of the send releases the lock ordering so both can complete.

The patch is **narrow**: `__del__` is only skipped during `_async_send`, which means object refs are merely *held* slightly longer than strictly necessary. No object ref is leaked — they're finalized normally at the next GC.

**Do not:** remove the patch, widen its scope (e.g. disabling `__del__` globally), or "fix the root cause" in Ray itself without upstream coordination. We've been living with this since before Ray 2.x; upstream has tracked it but not fixed it.

### 14.6 `clear_set_attrs()` reverts writes instead of blocking them

*Location:* `src/ndif/services/ray/nn/security/protected_objects.py`. See §7.9.

`ProtectedObject` *tracks* attribute writes on the model/tokenizer and reverts them after each request via `clear_set_attrs()` in `cleanup()` (§6.5). It does **not** refuse the writes at write time.

**Why not refuse?** Many transient writes during a legitimate request are safe — for example, nnsight may temporarily attach a hook to a module, then remove it. If `__setattr__` raised, those operations would break. Proving *at write time* which writes are safe and which are not would require a full data-flow analysis of nnsight internals, which is not tractable.

**Why is "revert after cleanup" sound?** The revert happens before the *next* user's request starts. No two users share state. A malicious write only sees itself during its own request, and the next request starts from a clean slate. This turns a static-analysis problem into a "just restore the snapshot" problem.

**Do not:** change `__setattr__` to raise. The test `test_security_guards.py` would still pass, but real nnsight flows would break in subtle ways that only manifest under specific intervention patterns.

### 14.7 The audit hook is permanent per-process

*Location:* `src/ndif/services/ray/nn/security/guards.py` — audit hook registration. See §7.7.

`sys.addaudithook` has a Python-level limitation: **you cannot remove an audit hook once installed**. The hook persists for the lifetime of the process.

NDIF installs the hook once at module import time and uses a `threading.local` flag (toggled by `Protector.__enter__` / `__exit__`) to decide whether the hook should *block* or *allow* a given call. When the sandbox is inactive, the flag is off and the hook short-circuits immediately — the overhead is one dict lookup and one `if`.

**Why:** because the hook cannot be removed, "enable during sandbox / disable outside" has to be done at *callback* time, not at registration time. The threading-local flag is the cheapest way to make it per-context.

**Do not:** try to remove or replace the audit hook at runtime. There is no supported way to do it. If you need a new category of blocked operation, add it to the hook's callback and gate it on the same flag.

### 14.8 CUDA device-side assertion triggers a terminal self-kill

*Location:* `src/ndif/services/ray/deployments/modeling/base.py` — `exception()` → `restart()`. See §6.5.

When user code triggers a CUDA device-side assertion, `BaseModelDeployment.exception()` detects the `"device-side assert triggered"` string and calls `restart()`, which invokes `ray.kill(actor, no_restart=False)` to have Ray respawn the actor from scratch.

**Why not catch and recover in-process?** A CUDA device-side assertion corrupts the CUDA context. Every subsequent CUDA call on the same process — even from correct code — returns the same error, because the context is permanently bad. The only way to clear the context is to create a new process. `ray.kill(no_restart=False)` tells Ray to terminate the actor and immediately start a fresh one; the new actor loads the model from disk and resumes serving.

**Do not:** wrap the offending call in `try/except` and continue. The next request will hit the same poisoned context and surface the same error, now masquerading as a bug in unrelated user code.

### 14.9 Thread kills use `ctypes`, not cooperative cancellation

*Location:* `src/ndif/services/ray/deployments/modeling/util.py` — `kill_thread()`. See §6.4.

`kill_thread()` uses `ctypes.PyThreadState_SetAsyncExc` to inject a `SystemExit` exception into a running Python thread. This is the mechanism used when the `kill_switch` fires or the execution timeout expires (§6.4).

**Why not cooperative cancellation (`threading.Event` the thread polls)?** User code is arbitrary Python. There is no way to force it to poll a cancellation flag. A tight `while True: pass` loop would hang forever despite `kill_switch.set()` being called millennia ago.

**Why `ctypes` at all?** Python does not expose a public API for killing a thread. `ctypes.PyThreadState_SetAsyncExc` is documented as internal and "for debugging" but has been stable across Python versions for over a decade. It is the only mechanism that (a) interrupts a tight loop and (b) does not require cooperation from the target.

**Known hazards.** If the target thread is inside a C extension when the async exception is raised, the exception is deferred until the extension returns to Python. This means a user who runs a 20-minute `torch.matmul` might get their timeout honored late. There is nothing to be done about this at the Python level.

**Do not:** replace with cooperative cancellation. It will silently fail to terminate real user code.

### 14.10 `UnauthorizedModule` defers errors until use

*Location:* `src/ndif/services/ray/nn/security/importer.py`. See §7.2.

Non-whitelisted imports do not raise `ImportError` at `import` time. They return an `UnauthorizedModule` — a lazy placeholder that raises only when the user actually *uses* it (attribute access, calling, etc.).

**Why lazy?** Because many whitelisted libraries perform speculative imports of optional dependencies inside `try: import X except ImportError: pass`. For example, `transformers` imports `flax` and `tf` opportunistically, catches `ImportError`, and proceeds with just `torch`. If `import flax` raised immediately, `transformers` would partially break even though the user's code never touches `flax`.

Deferring the error until *use* has the right semantics: whitelisted libraries that fall back cleanly still work, and user code that tries to actually call `flax.linen.Dense` gets a clean `ImportError` at the point of use.

**Do not:** raise at import time. Every whitelisted library that uses try-except imports will stop working.

### 14.11 `whitelist.yaml` edits require an image rebuild

*Location:* `src/ndif/services/ray/nn/security/whitelist.yaml`. See §7.11.

`whitelist.yaml` is **packaged into the Ray image at build time**, not bind-mounted. Editing it on disk and restarting containers does nothing — the container still has the old version baked in. You must run `make ta` (or at minimum rebuild the `ray` image) for a whitelist change to take effect.

**Why not bind-mount it for hot reloading?** Because in production the whitelist is a security policy that ships with the release. Bind-mounting it would make the deployed container's policy depend on the host filesystem, which is a supply-chain attack surface.

**Do not:** edit `whitelist.yaml` and expect existing containers to pick it up. Always `make ta` (or `make build` + `make up`) after whitelist changes, and always re-run `pytest tests/test_security_guards.py --run-remote` afterward.

### 14.12 The Processor detects eviction via the `"Failed to look up actor"` string

*Location:* `src/ndif/services/ray/deployments/modeling/base.py::__call__` raises `LookupError("Failed to look up actor")` when `self.cached`. Matched by `src/ndif/services/api/queue/processor.py::execute`. See §4.4.

When a `ModelActor` is cached to CPU (HOT → WARM), subsequent request dispatches to that actor raise `LookupError("Failed to look up actor")`. The Processor string-matches on this message to distinguish "my model got evicted" (recoverable, re-provision) from "Ray died" (unrecoverable, purge all Processors and reconnect).

**Why string matching on an error message?** Because the error crosses process + network boundaries as a Ray exception, and the type information is lossy by the time it reaches the Dispatcher. Matching on a distinctive string is the cheapest way to disambiguate two failure modes that otherwise look identical.

**Do not:** change the error message without also updating the Processor's match. If you rename this string, integration tests will still pass but evicted-model recovery will silently break — the Processor will treat eviction as a connection failure and purge everything.

### 14.13 Garbage collection runs every 5 requests, not every request

*Location:* `src/ndif/services/ray/deployments/modeling/base.py` — `cleanup()`. See §6.5.

`cleanup()` increments a counter and only calls `gc.collect()` when `self._request_count % 5 == 0`.

**Why not every request?** `gc.collect()` is an O(objects) operation; on a ModelActor holding a multi-billion-parameter model, the "reachable from a root" sweep walks millions of `torch.nn.Parameter` and `torch.Tensor` objects every time. Running it per request adds tens to hundreds of milliseconds of latency for almost no benefit — Python's reference counting handles the common case, and the allocator reuses memory between requests.

**Why not never?** Cyclic references (which refcounting can't free) do accumulate. Once every five requests is a heuristic tuned to "often enough that cycles don't pile up, rarely enough that latency isn't dominated by GC."

**Do not:** move `gc.collect()` back to every request. Benchmarks will get noticeably worse.

### 14.14 `cleanup()` does not call `torch.cuda.empty_cache()`

*Location:* same file, `cleanup()`. See §6.5.

PyTorch's caching allocator holds onto freed GPU memory and reuses it for subsequent allocations within the same process. Calling `torch.cuda.empty_cache()` releases memory back to the CUDA driver — which then has to re-acquire it on the next allocation. Between requests on the *same* actor, this trade is strictly worse: you pay the re-acquire cost every request and gain nothing, because no other process on the GPU is waiting for that memory.

**When *would* `empty_cache()` be right?** Only if another process shared the GPU and you wanted to release memory for its use. On NDIF each GPU is exclusively owned by one or more `ModelActor`s, and the memory budgets are set via `set_per_process_memory_fraction`. There is no other process to yield memory to.

**Do not:** add `empty_cache()` to `cleanup()` for "hygiene." It makes real throughput worse.

---

## 15. Configuration Appendix

This is the consolidated env-var reference. Every variable that NDIF code reads is listed here, grouped by subsystem, with the code default and the file that defines it. `.env.example` is the shipped set of development-friendly overrides; where it differs from the code default, both are noted.

For the subsystem-specific tables that appear inline in earlier sections (§3.5, §5.1, §12.3), think of them as curated quick-references — use them when you already know which part of the system you're working on. Use this appendix when you don't.

### 15.1 Core services

| Variable | Code default | `.env.example` | Read in | Description |
|---|---|---|---|---|
| `NDIF_BROKER_URL` | `redis://localhost:6379` | `redis://localhost:6379` | `common/providers/redis.py`, `api/src/config.py`, `api/src/queue/config.py` | Redis connection URL (queue, pub/sub, streams, Socket.IO backend) |
| `NDIF_BROKER_PORT` | — | `6379` | `.env.example` only | Port exposed by the redis container |
| `NDIF_OBJECT_STORE_URL` | — | `http://localhost:27018` | `common/providers/objectstore.py` | MinIO/S3 endpoint |
| `NDIF_OBJECT_STORE_PORT` | — | `27018` | `.env.example` | Host port for MinIO S3 API |
| `NDIF_OBJECT_STORE_CONSOLE_PORT` | — | `46805` | `.env.example` | Host port for MinIO console |
| `NDIF_OBJECT_STORE_SERVICE` | `s3` | `s3` | `common/providers/objectstore.py` | Service name for boto3 |
| `NDIF_OBJECT_STORE_BUCKET` | — | `ndif-results` | `common/providers/objectstore.py` | Bucket for results/responses |
| `NDIF_OBJECT_STORE_ACCESS_KEY` | — | `minioadmin` | `common/providers/objectstore.py` | MinIO access key |
| `NDIF_OBJECT_STORE_SECRET_KEY` | — | `minioadmin` | `common/providers/objectstore.py` | MinIO secret key |
| `NDIF_OBJECT_STORE_REGION` | — | `us-east-1` | `common/providers/objectstore.py` | S3 region |
| `NDIF_OBJECT_STORE_VERIFY` | `false` | `false` | `common/providers/objectstore.py` | Verify TLS certs |
| `NDIF_API_URL` | — | `http://localhost:5001` | `common/providers/socketio.py`, `cli/commands/start.py` | Public API URL |
| `NDIF_API_PORT` | — | `5001` | `.env.example`, compose | API listen port |
| `NDIF_API_WORKERS` | — | `1` | compose | Gunicorn worker count |
| `NDIF_DEV_MODE` | `false` | `true` | `api/src/db.py`, `api/src/config.py` | If true, bypass API-key validation and skip Postgres |

### 15.2 Ray cluster

| Variable | Code default | `.env.example` | Read in | Description |
|---|---|---|---|---|
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | `ray://localhost:10001` | `common/providers/ray.py`, `cli/commands/start.py` | Ray client connection address |
| `NDIF_RAY_HEAD_PORT` | — | `6385` | `.env.example`, compose | Ray head node port |
| `NDIF_RAY_CLIENT_PORT` | — | `10001` | `.env.example`, compose | Ray client protocol port |
| `NDIF_RAY_DASHBOARD_PORT` | — | `8265` | `.env.example`, compose | Ray dashboard HTTP port |
| `NDIF_RAY_SERVE_PORT` | — | `8262` | `.env.example`, compose | Ray Serve / metrics port |
| `NDIF_RAY_OBJECT_MANAGER_PORT` | — | `8076` | compose | Ray object manager port |
| `NDIF_RAY_DASHBOARD_GRPC_PORT` | — | `8268` | compose | Ray dashboard gRPC port |
| `NDIF_RAY_TEMP_DIR` | — | `/tmp/ray` | `.env.example`, compose | Ray temp/session dir |

### 15.3 Controller and cluster management

| Variable | Code default | `.env.example` | Read in | Description |
|---|---|---|---|---|
| `NDIF_CONTROLLER_IMPORT_PATH` | — | `ndif.services.ray.deployments.controller.controller` | compose | Python path to the Controller app factory (swap to `...gcal.controller` to enable Google Calendar scheduling) |
| `NDIF_DEPLOYMENTS` | `""` | — | `ray/deployments/controller/controller.py` | Pipe-separated list of model keys to deploy as dedicated at startup |
| `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | `3600` | — | `ray/deployments/controller/controller.py` | Max execution time per request unless overridden per-deployment |
| `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` | `0` | `ray/deployments/controller/controller.py` | Min time before a non-dedicated model can be evicted |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | — | `ray/deployments/controller/controller.py` | Fraction of CPU memory usable as WARM cache |
| `NDIF_DEFAULT_PADDING_FACTOR` | `0.15` | — | `ray/deployments/controller/controller.py` | Multiplicative memory-overhead padding (see §5.3) |
| `NDIF_DEFAULT_PADDING_BIAS` | `524288000` (500 MB) | — | `ray/deployments/controller/controller.py` | Additive memory-overhead padding |
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | — | `ray/deployments/controller/controller.py::check_nodes` | Interval for node discovery polling |

### 15.4 Google Calendar scheduling (`SchedulingControllerActor`)

Only read when `NDIF_CONTROLLER_IMPORT_PATH` points at the gcal variant. See §5.7.

| Variable | Code default | `.env.example` | Read in | Description |
|---|---|---|---|---|
| `SCHEDULING_GOOGLE_CALENDAR_ID` | `""` | — | `ray/deployments/controller/gcal/controller.py` | Calendar ID to poll |
| `SCHEDULING_GOOGLE_CREDS_PATH` | `""` | — | `ray/deployments/controller/gcal/controller.py` | Path to service-account JSON (inside the container) |
| `SCHEDULING_CHECK_INTERVAL_S` | `10` | — | `ray/deployments/controller/gcal/controller.py` | Calendar poll interval |
| `SCHEDULING_DELAY_START_S` | `15` | — | `ray/deployments/controller/gcal/controller.py` | Delay before first poll (lets workers join) |

### 15.5 Queue / Dispatcher

| Variable | Code default | `.env.example` | Read in | Description |
|---|---|---|---|---|
| `COORDINATOR_STATUS_CACHE_FREQ_S` | `120` | — | `api/src/queue/config.py` | How long `/status` responses are cached in Redis |
| `COORDINATOR_PROCESSOR_REPLY_FREQ_S` | `3` | — | `api/src/queue/config.py` | How often a Processor sends status updates to queued users |
| `STATUS_REQUEST_TIMEOUT_S` | `60` | — | `api/src/config.py` | Max wait for Controller status response |
| `SOCKETIO_MAX_HTTP_BUFFER_SIZE` | `100000000` | — | `api/src/config.py` | Max Socket.IO message size (100 MB) |
| `SOCKETIO_PING_TIMEOUT` | `60` | — | `api/src/config.py` | Socket.IO ping timeout |
| `MIN_NNSIGHT_VERSION` | installed version | — | `api/src/config.py` | Reject clients below this nnsight version |
| `MIN_PYTHON_VERSION` | installed version | — | `api/src/config.py` | Reject clients below this Python version |

### 15.6 Providers (shared)

| Variable | Code default | `.env.example` | Read in | Description |
|---|---|---|---|---|
| `PROVIDER_MAX_RETRIES` | `3` | — | `common/providers/__init__.py` | Retries for provider connects |
| `PROVIDER_RETRY_INTERVAL_S` | `5` | — | `common/providers/__init__.py` | Retry interval (seconds) |
| `MAILGUN_DOMAIN` | — | — | `common/providers/mailgun.py` | Mailgun domain (gcal error emails, non-blocking callbacks) |
| `MAILGUN_API_KEY` | — | — | `common/providers/mailgun.py` | Mailgun API key |

### 15.7 PostgreSQL (auth)

Skipped when `NDIF_DEV_MODE=true`.

| Variable | Code default | `.env.example` / compose | Read in | Description |
|---|---|---|---|---|
| `POSTGRES_HOST` | `localhost` | `postgres` (compose) | `common/providers/postgres.py` | PostgreSQL host |
| `POSTGRES_PORT` | `5432` | `5432` | `common/providers/postgres.py` | PostgreSQL port |
| `POSTGRES_DB` | `ndif` | `keys` (compose) | `common/providers/postgres.py` | Database name |
| `POSTGRES_USER` | `""` | `postgres` (compose) | `common/providers/postgres.py` | Database user |
| `POSTGRES_PASSWORD` | `""` | `postgres` (compose) | `common/providers/postgres.py` | Database password |
| `POSTGRES_MIN_CONNECTIONS` | `1` | — | `common/providers/postgres.py` | Connection pool minimum |
| `POSTGRES_MAX_CONNECTIONS` | `10` | — | `common/providers/postgres.py` | Connection pool maximum |

### 15.8 Telemetry

| Variable | Code default | `.env.example` / compose | Read in | Description |
|---|---|---|---|---|
| `INFLUXDB_ADDRESS` | — | `http://<host>:<DEV_INFLUXDB_PORT>` | `common/metrics/metric.py` | InfluxDB URL (unset = metrics no-op) |
| `INFLUXDB_ADMIN_TOKEN` | — | — | `common/metrics/metric.py` | InfluxDB token |
| `INFLUXDB_ORG` | — | — | `common/metrics/metric.py` | InfluxDB organization |
| `INFLUXDB_BUCKET` | — | — | `common/metrics/metric.py` | InfluxDB bucket |
| `LOKI_URL` | — | `http://<host>:<DEV_LOKI_PORT>/loki/api/v1/push` | `common/logging/logger.py` | Loki push URL (unset = local logs only) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | `http://<host>:<JAEGER_OTLP_GRPC_PORT>` | `common/tracing/setup.py` | OTLP gRPC endpoint for traces (unset = tracing no-op) |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5` | — | `common/tracing/setup.py` | OTLP exporter timeout in seconds |

### 15.9 Monitor service (§13)

Read only by the standalone `src/ndif/services/monitor/jobs/monitor.py`. These do **not** affect the main NDIF stack.

| Variable | Default | Description |
|---|---|---|
| `INSTALL_DIR` | `~/ndif_monitor` | Where monitor source, config, and logs live |
| `NDIF_API_KEY` | — | API key used for nnsight remote traces from the monitor |
| `MONITOR_CRON` | `*/10 * * * *` | Cron schedule for the monitor job |

### 15.10 CLI-only

Read by the `ndif` CLI when bringing up native-mode dependencies.

| Variable | Default | Description |
|---|---|---|
| `NDIF_SESSION_ROOT` | `~/.ndif` | Where CLI sessions are stored |
| `MICROMAMBA_DIR` | `~/.local/bin` | Where `cli/lib/deps.py` bootstraps micromamba to install Redis/MinIO |

---

## File Structure Reference

```
ndif/
├── cli/
│   ├── cli.py                          # Main CLI entry point (Click)
│   ├── config.py                       # Environment variable defaults
│   ├── commands/
│   │   ├── start.py                    # ndif start
│   │   ├── stop.py                     # ndif stop
│   │   ├── restart.py                  # ndif restart
│   │   ├── deploy.py                   # ndif deploy
│   │   ├── evict.py                    # ndif evict
│   │   ├── status.py                   # ndif status
│   │   ├── logs.py                     # ndif logs
│   │   ├── queue.py                    # ndif queue
│   │   ├── kill.py                     # ndif kill
│   │   ├── info.py                     # ndif info
│   │   └── env.py                      # ndif env
│   └── lib/
│       ├── session.py                  # Session management
│       ├── checks.py                   # Pre-flight checks
│       ├── deps.py                     # Redis/MinIO startup
│       └── util.py                     # Helpers
│
├── src/
│   ├── services/
│   │   ├── api/
│   │   │   ├── src/
│   │   │   │   ├── app.py              # FastAPI application
│   │   │   │   ├── config.py           # API configuration
│   │   │   │   ├── dependencies.py     # Request validation
│   │   │   │   ├── db.py               # PostgreSQL API key store
│   │   │   │   ├── gunicorn.conf.py    # Gunicorn configuration
│   │   │   │   ├── tracing/            # OpenTelemetry wiring for API spans
│   │   │   │   └── queue/
│   │   │   │       ├── dispatcher.py   # Central request coordinator
│   │   │   │       ├── processor.py    # Per-model lifecycle manager
│   │   │   │       ├── config.py       # Queue configuration
│   │   │   │       └── util.py         # Ray client helpers + deadlock patch
│   │   │   ├── tests/                  # API unit tests
│   │   │   ├── start.sh                # API startup script
│   │   │   └── requirements.in         # API dependencies
│   │   │
│   │   ├── ray/
│   │   │   ├── src/ray/
│   │   │   │   ├── start.py            # Controller startup
│   │   │   │   ├── resources.py        # Resource detection
│   │   │   │   ├── deployments/
│   │   │   │   │   ├── controller/
│   │   │   │   │   │   ├── controller.py       # ControllerActor
│   │   │   │   │   │   ├── cluster/
│   │   │   │   │   │   │   ├── cluster.py      # Cluster state
│   │   │   │   │   │   │   ├── node.py         # Node + GPU accounting
│   │   │   │   │   │   │   ├── deployment.py   # HOT/WARM/COLD lifecycle
│   │   │   │   │   │   │   └── evaluator.py    # Model size evaluation
│   │   │   │   │   │   └── gcal/                # Google Calendar scheduler
│   │   │   │   │   │       ├── controller.py   # SchedulingControllerActor
│   │   │   │   │   │       └── scheduler.py    # SchedulingActor (calendar poll)
│   │   │   │   │   └── modeling/
│   │   │   │   │       ├── base.py             # ModelActor / BaseModelDeployment
│   │   │   │   │       └── util.py             # kill_thread, accelerate helpers
│   │   │   │   └── nn/
│   │   │   │       ├── backend.py              # RemoteExecutionBackend
│   │   │   │       ├── ops.py                  # StdoutRedirect
│   │   │   │       └── security/               # ⚠️ read README.md before editing
│   │   │   │           ├── protector.py        # Protector context manager
│   │   │   │           ├── importer.py         # Importer, SandboxFinder, ProtectedModule
│   │   │   │           ├── guards.py           # guarded getattr, audit hook, restricted compile/exec
│   │   │   │           ├── protected_objects.py # ProtectedObject (model wrapper)
│   │   │   │           ├── whitelist.py        # Loads whitelist.yaml into typed consts
│   │   │   │           ├── whitelist.yaml      # Policy (modules, builtins, dunders, blocked submodules)
│   │   │   │           └── README.md           # Threat model + layer reference
│   │   │   ├── start.sh                # Ray head startup script
│   │   │   └── start-worker.sh         # Ray worker startup script
│   │   │
│   │   ├── monitor/                    # Standalone uptime monitor (§13)
│   │   │   ├── run.sh                  # Setup + cron deploy
│   │   │   ├── config.example.json     # Discord webhook + message templates
│   │   │   ├── jobs/
│   │   │   │   ├── monitor.py          # Cron job (connectivity + model traces)
│   │   │   │   └── util.py             # Config, discord, log rotation
│   │   │   ├── dashboard/
│   │   │   │   ├── dashboard.py        # Flask server
│   │   │   │   └── dashboard.html      # Frontend (Chart.js)
│   │   │   └── README.md
│   │   │
│   │   └── base/
│   │       └── requirements.in         # Shared base dependencies
│   │
│   └── common/
│       ├── types.py                    # Type aliases (MODEL_KEY, API_KEY, etc.)
│       ├── logging/
│       │   └── logger.py               # Centralized logging setup (Loki-aware)
│       ├── tracing/                    # OpenTelemetry wiring (shared by API + Ray)
│       │   ├── setup.py                # init_tracing, OTLP exporter
│       │   ├── spans.py                # trace_span, set_request_attributes
│       │   └── context.py              # TracingContext (propagation)
│       ├── metrics/
│       │   ├── metric.py               # Base metric class
│       │   ├── gpu_mem.py              # GPU memory tracking
│       │   ├── model_load_time.py      # Load time metrics
│       │   ├── network_data.py         # Network I/O metrics
│       │   ├── request_execution_time.py
│       │   ├── request_response_size.py
│       │   └── request_status_time.py
│       ├── providers/
│       │   ├── redis.py                # Redis client (sync + async)
│       │   ├── objectstore.py          # MinIO/S3 client
│       │   ├── socketio.py             # Socket.IO client
│       │   ├── mailgun.py              # Email notifications
│       │   ├── postgres.py             # PostgreSQL connection pool (auth)
│       │   └── ray.py                  # Ray connection management
│       └── schema/
│           ├── request.py              # BackendRequestModel
│           ├── response.py             # BackendResponseModel
│           ├── result.py               # BackendResultModel
│           ├── deployment_config.py    # DeploymentConfig (dedicated, timeouts, ...)
│           └── mixins.py               # ObjectStorageMixin, TelemetryMixin
│
├── cli/
│   ├── cli.py                          # Click entry point
│   ├── config.py                       # ENV_VARS resolution
│   ├── config/                         # Shipped model configs
│   ├── commands/
│   │   ├── start.py   stop.py   restart.py
│   │   ├── deploy.py  evict.py  kill.py
│   │   ├── status.py  queue.py  logs.py
│   │   ├── info.py    env.py    export.py
│   │   └── __init__.py
│   ├── lib/
│   │   ├── session.py                  # SessionConfig, ~/.ndif/ layout
│   │   ├── checks.py                   # Pre-flight checks
│   │   ├── deps.py                     # Redis/MinIO micromamba bootstrap
│   │   ├── model_config.py             # Model config loader
│   │   └── util.py
│   ├── tests/
│   └── README.md
│
├── docker/
│   ├── Dockerfile                      # Multi-purpose (NAME=api or NAME=ray)
│   ├── docker-compose.yml              # Full stack orchestration
│   └── postgres/
│       └── init.sql                    # Dev-mode keys DB schema + test key
│
├── telemetry/
│   ├── grafana/
│   │   ├── dashboards/                 # Pre-configured dashboards
│   │   └── provisioning/               # Grafana data sources
│   └── prometheus/
│       └── prometheus.yml              # Scrape configuration
│
├── tests/
│   ├── conftest.py                     # Remote-test skip logic
│   ├── test_nnsight.py                 # NNsight remote feature tests
│   ├── test_security_guards.py         # Sandbox validation
│   ├── test_user_code.py               # User code (de)serialization
│   ├── test_hotswapping.py             # Scheduler / eviction / fractional GPUs
│   └── reconnection/                   # Ray failure recovery tests
│
├── scripts/
│   ├── test.py                         # Smoke: GPT-2 trace via local API
│   └── redeploy.py
│
├── pyproject.toml                      # Python 3.12+, uv-managed
├── Makefile                            # Build + run (resolves NNSIGHT_PATH)
├── .env.example                        # Default configuration
├── NDIF.md                             # This file (design doc, for humans)
├── CLAUDE.md                           # Agent guide
└── README.md                           # Project overview
```
