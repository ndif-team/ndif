# NDIF: Design and Implementation

*Jaden Fiotto-Kaufman*

---

## Goal of This Document

This document provides an overview of the design choices and implementation details of NDIF (National Deep Inference Fabric). Its purpose is to serve as a **source of truth** for understanding how NDIF works internally, enabling developers and contributors to reason correctly about its behavior.

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
4. [Queue System](#4-queue-system)
   - [Overview](#overview-2)
   - [The Dispatcher](#41-the-dispatcher)
   - [The Processor](#42-the-processor)
   - [Request Lifecycle](#43-request-lifecycle)
   - [Error Handling and Recovery](#44-error-handling-and-recovery)
5. [Ray Service](#5-ray-service)
   - [Overview](#overview-3)
   - [The Controller](#51-the-controller)
   - [Cluster Management](#52-cluster-management)
   - [Model Evaluation](#53-model-evaluation)
   - [Deployment Levels](#54-deployment-levels)
   - [Deployment Scheduling](#55-deployment-scheduling)
   - [The Build/Apply Cycle](#56-the-buildapply-cycle)
6. [Model Execution](#6-model-execution)
   - [Overview](#overview-4)
   - [The ModelActor](#61-the-modelactor)
   - [Request Deserialization](#62-request-deserialization)
   - [Execution Pipeline](#63-execution-pipeline)
   - [Timeout and Cancellation](#64-timeout-and-cancellation)
   - [Cleanup and Memory Management](#65-cleanup-and-memory-management)
   - [Streaming and Logging](#66-streaming-and-logging)
7. [Security](#7-security)
   - [Overview](#overview-5)
   - [The Protector](#71-the-protector)
   - [Import Restrictions](#72-import-restrictions)
   - [Builtin Restrictions](#73-builtin-restrictions)
   - [Dunder Attribute Guards](#74-dunder-attribute-guards)
   - [Module Immutability](#75-module-immutability)
   - [Protected Objects](#76-protected-objects)
   - [Restricted Compile and Exec](#77-restricted-compile-and-exec)
   - [The Whitelist](#78-the-whitelist)
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

NDIF consists of three main services:

| Service | Role | Technology |
|---------|------|-----------|
| **API** | HTTP gateway, request validation, queue management | FastAPI, Gunicorn, Socket.IO |
| **Ray** | Distributed compute, model deployment, execution | Ray Actors |
| **CLI** | Service management, monitoring, deployment commands | Click |

And four external dependencies:

| Dependency | Role |
|-----------|------|
| **Redis** | Message queue, pub/sub, caching |
| **MinIO** | S3-compatible object storage for results |
| **PostgreSQL** | API key storage and tier management |
| **Prometheus/Grafana** | Metrics and monitoring |

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
  │  brpop with 1s timeout
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
- `src/services/api/src/app.py` — FastAPI application and endpoints
- `src/services/api/src/dependencies.py` — Request validation functions
- `src/services/api/src/config.py` — Environment-based configuration
- `src/services/api/src/db.py` — PostgreSQL API key store
- `src/services/api/src/queue/` — Dispatcher and Processor

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

---

## 4. Queue System

### Overview

The queue system is responsible for routing requests from the API endpoint to the Ray cluster. It consists of two main classes:

- **Dispatcher** — A singleton that reads from the Redis queue and routes requests to Processors
- **Processor** — A per-model coordinator that manages deployment lifecycle and request execution

**Key files:**
- `src/services/api/src/queue/dispatcher.py`
- `src/services/api/src/queue/processor.py`
- `src/services/api/src/queue/config.py`
- `src/services/api/src/queue/util.py`

### 4.1 The Dispatcher

The Dispatcher is the central coordinator. It runs as a long-lived asyncio event loop with several concurrent tasks:

```
┌─────────────────────────────────────────────────────────┐
│                    Dispatcher                            │
│                                                          │
│  dispatch_worker (main loop)                            │
│    │  1. brpop from Redis "queue" (1s timeout)          │
│    │  2. Route request to Processor by model_key        │
│    │  3. Handle evictions and errors                    │
│    │                                                    │
│  status_worker (background)                             │
│    │  Listen for status triggers, query Controller      │
│    │                                                    │
│  events_worker (background)                             │
│    │  Handle deploy/evict/kill/env events from Redis    │
│    │  stream "dispatcher:events"                        │
│                                                          │
│  processors: Dict[model_key, Processor]                 │
│  error_queue: shared queue for Processor errors         │
│  eviction_queue: shared queue for Processor evictions   │
└─────────────────────────────────────────────────────────┘
```

**Startup sequence:**

1. `Dispatcher.start()` creates an instance and calls `asyncio.run(dispatch_worker())`
2. The constructor connects to Redis and Ray (with retry logic)
3. Sets `ray:connected` in Redis to signal the API that Ray is available
4. Spawns `status_worker` and `events_worker` as background asyncio tasks
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

External commands (from the CLI or other tools) are delivered via the Redis stream `dispatcher:events`. The `events_worker` handles:

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

**Ray client deadlock workaround:** The Dispatcher applies a patch to Ray's `DataClient._async_send` to prevent a deadlock where a `ClientObjectRef` deletion during an async send causes both operations to compete for the same lock.

---

## 5. Ray Service

### Overview

The Ray service runs as a Ray cluster with a head node hosting the Controller actor and worker nodes hosting ModelActors. The Controller is the brain of the cluster — it tracks resources, manages deployments, and coordinates model lifecycle transitions.

**Key files:**
- `src/services/ray/src/ray/start.py` — Ray startup script
- `src/services/ray/src/ray/deployments/controller/controller.py` — Controller actor
- `src/services/ray/src/ray/deployments/controller/cluster/` — Cluster state management
- `src/services/ray/src/ray/deployments/modeling/base.py` — ModelActor

### 5.1 The Controller

The Controller is a Ray actor that runs on the head node. It is defined as a detached actor with unlimited restarts:

```python
@ray.remote(num_cpus=1, num_gpus=0, max_restarts=-1, resources={"head": 1})
class ControllerActor(_ControllerActor):
    pass
```

It is instantiated with configuration from environment variables:

| Parameter | Env Variable | Default | Description |
|-----------|-------------|---------|-------------|
| `deployments` | `NDIF_DEPLOYMENTS` | `""` | Pipe-separated model keys to deploy at startup (each gets default DeploymentConfig) |
| `minimum_deployment_time_seconds` | `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` | Min time a model stays deployed before eviction |
| `model_cache_percentage` | `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | Fraction of CPU memory available for warm cache |

Execution timeout is per-deployment via `DeploymentConfig.execution_timeout_seconds` (default from `NDIF_EXECUTION_TIMEOUT_SECONDS`).

**Key responsibilities:**

1. **Cluster state** — Track nodes, GPUs, and memory via periodic `update_nodes()` calls
2. **Deployment management** — Handle `deploy()` and `evict()` requests from the Dispatcher
3. **Status reporting** — Provide cluster and model status via `status()` method
4. **Environment info** — Report Python version and installed packages via `env()`

### 5.2 Cluster Management

The `Cluster` class maintains the authoritative view of the cluster state:

```python
class Cluster:
    nodes: Dict[NODE_ID, Node]     # GPU and CPU-bearing nodes
    resource_evaluator: ResourceEvaluator  # Model size cache
    # Head node is always excluded from model placement
```

**Node discovery:**

The `update_nodes()` method polls Ray's `list_nodes()` API every 30 seconds (configurable via `NDIF_CONTROLLER_SYNC_INTERVAL_S`). It:

1. Queries all nodes with `detail=True`
2. Includes both GPU and CPU-only nodes (CPU nodes support CPU-only deployments)
3. Marks head node via `resources_total["head"]` (set by `ray start --head --resources`)
4. For new nodes: creates a `Node` with `CPUResource` and `GPUResource` tracking
5. For removed nodes: purges all deployments and frees resources

Each `Node` tracks `cpu_resource` and `gpu_resource`:

```python
@dataclass
class CPUResource:
    cpu_memory_bytes: int              # CPU memory for caching (total * cache_percentage)
    available_cpu_memory_bytes: int    # Remaining CPU cache capacity

@dataclass
class GPUResource:
    total_gpus: int                    # Total GPU count
    gpu_memory_bytes: int              # Memory per GPU
    available_gpus: list[int]          # List of available GPU indices
```

**Deployment algorithm:**

When `deploy()` is called with `Dict[MODEL_KEY, DeploymentConfig]`:

1. **Evaluate** each model's size via `ResourceEvaluator` (loads model on meta device, sums parameter sizes, adds padding from `DeploymentConfig.padding_factor`)
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

The `ModelEvaluator` determines how many GPUs a model needs:

```python
class ModelEvaluator:
    padding_factor: float = 0.15    # 15% memory overhead
    cache: Dict[MODEL_KEY, CacheEntry]

    def __call__(self, model_key: MODEL_KEY) -> Union[float, Exception]:
        if model_key in self.cache:
            return self.cache[model_key].size_in_bytes

        meta_model = RemoteableMixin.from_model_key(model_key, dispatch=False)
        param_size = sum(p.nelement() * p.element_size() for p in meta_model._model.parameters())
        buffer_size = sum(b.nelement() * b.element_size() for b in meta_model._model.buffers())
        total = (param_size + buffer_size) * (1 + self.padding_factor)

        self.cache[model_key] = CacheEntry(total, n_params, config, revision)
        return total
```

The model is loaded on a meta device (no actual weights) using `RemoteableMixin.from_model_key()`. This gives us the exact parameter counts and dtypes without requiring GPU memory.

**GPU calculation:**

```python
def gpus_required(self, model_size_in_bytes: int) -> int:
    return int(model_size_in_bytes // self.gpu_memory_bytes + 1)
```

This divides the model size by per-GPU memory and rounds up.

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

The eviction algorithm in `Node.get_gpu_evictions()` (and `get_cpu_evictions()` for CPU-only deployments):

```python
def get_gpu_evictions(self, gpus_required: int, dedicated: bool = False) -> List[MODEL_KEY]:
    deployments = sorted(self.deployments.values(), key=lambda x: len(x.gpus))
    gpus_needed = gpus_required - len(self.gpu_resource.available_gpus)
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

The ordering is critical: deletions and caches must complete before from-cache and creates, because they free the GPU resources needed by the new deployments.

---

## 6. Model Execution

### Overview

The `ModelActor` is a Ray actor that holds a loaded model and executes user intervention code. It handles the complete lifecycle of a request: deserialization, sandboxed execution, result serialization, and cleanup.

**Key files:**
- `src/services/ray/src/ray/deployments/modeling/base.py` — BaseModelDeployment and ModelActor
- `src/services/ray/src/ray/nn/backend.py` — RemoteExecutionBackend
- `src/services/ray/src/ray/nn/ops.py` — StdoutRedirect

### 6.1 The ModelActor

```python
@ray.remote(num_cpus=2, num_gpus=0, max_restarts=-1)
class ModelActor(BaseModelDeployment):
    pass
```

Note: `num_gpus=0` because GPU assignment is handled via `CUDA_VISIBLE_DEVICES` environment variable, not Ray's GPU scheduling. This gives the Controller precise control over which physical GPUs each actor uses.

**Initialization (`__init__`):**

1. Connect to MinIO and Socket.IO providers
2. Set default dtype to `bfloat16`
3. Load model from disk via `RemoteableMixin.from_model_key()` with `device_map="auto"`
4. Extract persistent objects (tokenizer, etc.) that should be available during execution
5. Protect persistent objects — wrap `torch.nn.Module` objects in `ProtectedObject` to prevent modification
6. Create the `Protector` for sandboxed execution
7. Disable gradients on the model (`requires_grad_(False)`)
8. Initialize a single-threaded `ThreadPoolExecutor` for execution

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
            timeout=self.execution_timeout_seconds,
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
    raise Exception(f"Job took longer than timeout: {self.execution_timeout_seconds} seconds")
```

`kill_thread()` uses `ctypes` to raise a `SystemExit` exception in the target thread. This is a last resort — it's not clean, but it prevents runaway execution from blocking the actor.

### 6.5 Cleanup and Memory Management

After every request (success or failure), `cleanup()` runs:

```python
def cleanup(self):
    self.kill_switch.clear()
    self.execution_ident = None
    self.model._model.zero_grad()
    self.request = None
    gc.collect()
    torch.cuda.empty_cache()
```

This ensures:
- Gradients don't accumulate across requests
- The request reference is released
- Python garbage collection runs
- CUDA memory cache is cleared

For CUDA device-side assertion errors (which corrupt the CUDA context), the actor restarts itself:

```python
if "device-side assert triggered" in str(exception):
    ray.kill(ray.get_actor(f"ModelActor:{self.model_key}", namespace="NDIF"), no_restart=False)
```

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

---

## 7. Security

### Overview

NDIF executes arbitrary user-submitted Python code on shared GPU infrastructure. The security model must prevent:

1. **Filesystem access** — No reading/writing files
2. **Network access** — No outbound connections
3. **Model modification** — No changing model weights
4. **Sandbox escape** — No accessing internals via `__class__`, `__globals__`, etc.
5. **Resource abuse** — No fork bombs, infinite loops (handled by timeout)

The security system uses a layered approach combining RestrictedPython guards, import restrictions, builtin restrictions, and object protection.

**Key files:**
- `src/services/ray/src/ray/nn/security/protected_environment.py`
- `src/services/ray/src/ray/nn/security/protected_objects.py`
- `src/services/ray/src/ray/nn/security/whitelist.yaml`

### 7.1 The Protector

The `Protector` class is the central security mechanism. It extends NNsight's `Patcher` class to apply and remove patches as a context manager:

```python
class Protector(Patcher):
    def __init__(self, whitelisted_modules, builtins=False, restrict_compile=True):
        # Patch __import__ to use custom Importer
        self.add(Patch(__builtins__, replacement=self.importer, key="__import__"))

        # Patch StreamTracer.execute to temporarily escape the sandbox
        self.add(Patch(StreamTracer, replacement=self.escape(StreamTracer.execute), key="execute"))

        # Patch compile and exec to restricted versions
        if restrict_compile:
            self.add(Patch(__builtins__, replacement=restricted_compile, key="compile"))
            self.add(Patch(__builtins__, replacement=restricted_exec, key="exec"))
```

When used as a context manager (`with Protector(...):`), all patches are applied on entry and removed on exit. This means NNsight's internal code runs unrestricted, and only the user's intervention function runs in the sandbox.

The `escape()` method wraps a function to temporarily exit the sandbox during its execution. This is used for `StreamTracer.execute()`, which needs unrestricted access to NNsight internals.

### 7.2 Import Restrictions

The `Importer` class replaces Python's `__import__`:

```python
class Importer:
    def __call__(self, name, globals, locals, fromlist, level):
        if name in ["builtins", "__builtins__"]:
            return PROTECTED_BUILTINS

        for module in self.whitelisted_modules:
            if module.check(name):
                # Import normally, then wrap in ProtectedModule
                result = self.original_import(name, ...)
                protected = ProtectedModule(module)
                protected.__dict__.update(result.__dict__)
                return protected

        return UnauthorizedModule(name)
```

Key behaviors:

1. **Whitelisted modules** are imported normally but wrapped in `ProtectedModule` (immutable)
2. **Non-whitelisted modules** return an `UnauthorizedModule` — a lazy placeholder that raises `ImportError` only when the user tries to *use* it (attribute access, calling, etc.)
3. **Relative imports** (`from . import x`) temporarily exit the sandbox to resolve, then re-wrap
4. **Builtins access** returns a `SafeBuiltins` wrapper

The lazy `UnauthorizedModule` design means that importing a non-whitelisted module doesn't immediately fail — this handles cases where libraries import optional dependencies that the user doesn't actually use.

### 7.3 Builtin Restrictions

Two levels of builtin restriction:

**`SafeBuiltins` (always active):** A whitelist of allowed builtins used as `__builtins__` in restricted exec. Includes standard types (`int`, `str`, `list`, etc.), iteration (`range`, `enumerate`, `zip`), and exceptions — but **excludes** `open`, `__import__` (replaced), `eval` (in exec only), and `exec`/`compile` (replaced with restricted versions).

**Full builtin patching (when `builtins=True`):** Removes non-whitelisted builtins from the actual `__builtins__` dict. This is the most restrictive mode, used during execution.

### 7.4 Dunder Attribute Guards

RestrictedPython-style guards are injected into the execution globals to intercept attribute access:

**`guarded_getattr`** — Blocks access to dangerous dunder attributes:

```python
def guarded_getattr(obj, name, default=None):
    if name.startswith("_"):
        if name in ALLOWED_DUNDER_ATTRS:
            pass                              # Safe dunders (e.g., __len__, __iter__)
        elif name in BLOCKED_DUNDER_ATTRS:
            raise AttributeError(...)          # Dangerous (e.g., __class__, __globals__)
        else:
            raise AttributeError(...)          # All other private attrs
    return getattr(obj, name)
```

**Blocked dunders** include the classic sandbox escape vectors:
- `__class__`, `__bases__`, `__mro__`, `__subclasses__` — type system access
- `__globals__`, `__code__`, `__closure__` — function internals
- `__dict__`, `__getattribute__` — arbitrary attribute access
- `__reduce__`, `__reduce_ex__` — pickle-based code execution

**Allowed dunders** include safe operations:
- Arithmetic: `__add__`, `__sub__`, `__mul__`, etc.
- Container: `__len__`, `__iter__`, `__getitem__`, `__setitem__`
- Context managers: `__enter__`, `__exit__`
- Comparison: `__eq__`, `__lt__`, etc.

### 7.5 Module Immutability

`ProtectedModule` wraps imported modules to prevent modification:

```python
class ProtectedModule(ModuleType):
    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if isinstance(attr, ModuleType):
            # Check that submodule is within whitelist scope
            if not allowed:
                raise AttributeError(f"Module attribute {attr.__name__} is not whitelisted")
            return ProtectedModule(...)  # Recursively protect
        return attr

    def __setattr__(self, name, value):
        raise AttributeError("Cannot modify protected module")

    def __delattr__(self, name):
        raise AttributeError("Cannot modify protected module")
```

This prevents users from monkey-patching whitelisted modules (e.g., `torch.save = my_exfil_function`).

The `strict` flag on `WhitelistedModule` controls submodule access:
- `strict=True`: Only the exact module name is allowed (e.g., `operator` but not `operator.attrgetter`)
- `strict=False`: The module and all submodules are allowed (e.g., `torch` includes `torch.nn`, `torch.cuda`, etc.)

### 7.6 Protected Objects

Persistent objects (like the model's tokenizer) are wrapped in `ProtectedObject`:

```python
class ProtectedObject:
    def __getattribute__(self, name):
        if name in ["to"]:
            raise ValueError(f"Attribute `{name}` cannot be accessed")

        value = getattr(PROTECTIONS[id(self)], name)

        if isinstance(value, (torch.Tensor, list, dict)):
            return deepcopy(value)  # Return a copy, not the original

        return value

    def __setattr__(self, name, value):
        raise AttributeError("Cannot modify protected object")
```

Key protections:
- `.to()` is blocked to prevent moving the model to a different device
- Tensor, list, and dict attributes are deep-copied to prevent in-place modification of the original
- All attribute setting is blocked after initialization

### 7.7 Restricted Compile and Exec

When user code calls `compile()` or `exec()` within the sandbox, restricted versions are used:

```python
def restricted_compile(source, filename="<restricted>", mode="exec", ...):
    return _original_compile(source, filename, mode, flags, ...)

def restricted_exec(code, globals=None, locals=None):
    exec_globals = make_restricted_globals(globals)  # Inject guards
    _original_exec(code, exec_globals, locals)
```

`make_restricted_globals()` injects the guard functions that RestrictedPython's AST transformations expect:
- `_getattr_` → `guarded_getattr`
- `_getitem_` → `default_guarded_getitem`
- `_getiter_` → `default_guarded_getiter`
- `_write_` → `GuardedWrite` wrapper
- `_inplacevar_` → Safe in-place operation handler

Note: The compile step uses standard Python `compile()`, not RestrictedPython's AST transformation. This is because RestrictedPython blocks NNsight's internal variable names (like `__nnsight_tracer_*`). Security is enforced at runtime through the guards instead.

### 7.8 The Whitelist

The whitelist is defined in `whitelist.yaml` and loaded at module import time:

**Allowed modules during execution:**
- `torch` (and all submodules)
- `numpy`, `collections`, `math`, `time`, `typing`
- `einops`, `sympy`, `pandas`, `enum`
- `nnsight.intervention.envoy` (for Envoy access)
- `operator`, `_operator`

**Additional modules during deserialization only:**
- `pickle`, `cloudpickle`, `copyreg`
- `nnsight.schema.request`, `nnsight.modeling`, `nnsight.intervention.*`
- `transformers` (and all submodules)

**Notable exclusions:**
- `os`, `sys`, `subprocess` — Filesystem and process access
- `socket`, `http`, `urllib` (except `urllib3` for library compatibility) — Network access
- `importlib` — Dynamic import manipulation
- `ctypes` — Arbitrary memory access
- `builtins` is available but wrapped in `SafeBuiltins`

---

## 8. Schema and Data Models

### Overview

NDIF uses Pydantic models for request/response serialization. These models handle the full lifecycle of data: receipt from the client, storage in object stores, and delivery back to the client.

**Key files:**
- `src/common/schema/request.py` — `BackendRequestModel`
- `src/common/schema/response.py` — `BackendResponseModel`
- `src/common/schema/result.py` — `BackendResultModel`
- `src/common/schema/mixins.py` — `ObjectStorageMixin`, `TelemetryMixin`
- `src/common/types.py` — Type aliases

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
- `src/common/providers/redis.py` — Redis connections
- `src/common/providers/objectstore.py` — MinIO/S3 connections
- `src/common/providers/socketio.py` — Socket.IO client
- `src/common/providers/mailgun.py` — Email notifications

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

### 9.4 PostgreSQL

PostgreSQL stores API keys and tier assignments. The `AccountsDB` class provides:

```python
class AccountsDB:
    def api_key_exists(self, key_id: API_KEY) -> bool
    def key_has_hotswapping_access(self, key_id: API_KEY) -> bool
```

In dev mode (`NDIF_DEV_MODE=true`), the database is not initialized and all keys are accepted.

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
| `ndif deploy --deployment-config <file>` | Deploy models from a JSON/YAML config file (per-model settings) |
| `ndif evict <model_key>` | Evict a model from the cluster |
| `ndif logs <service>` | View service logs |
| `ndif queue` | Show queue state (pending requests per model) |
| `ndif kill <request_id>` | Cancel a specific request |
| `ndif info` | Show current session information |
| `ndif env` | Show Ray cluster Python environment |

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
COPY src/services/${NAME}/requirements.in /tmp/requirements.in
RUN uv pip install --system -r /tmp/requirements.in

RUN --mount=type=bind,source=/src,target=/tmp/src \
    cp -rL /tmp/src/services/${NAME}/src / && \
    cp /tmp/src/services/${NAME}/start.sh ./start.sh

CMD bash ./start.sh
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

All configuration is via environment variables. Defaults are in `.env.example`:

**Core Services:**

| Variable | Default | Description |
|----------|---------|-------------|
| `NDIF_BROKER_URL` | `redis://localhost:6379/` | Redis connection URL |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:27017` | MinIO S3 endpoint |
| `NDIF_API_PORT` | `8001` | API server port |
| `NDIF_API_WORKERS` | `1` | Gunicorn worker count |
| `NDIF_DEV_MODE` | `false` | Skip API key validation |

**Ray Cluster:**

| Variable | Default | Description |
|----------|---------|-------------|
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | Ray client connection address |
| `NDIF_RAY_HEAD_PORT` | `6379` | Ray head node port |
| `NDIF_RAY_DASHBOARD_PORT` | `8265` | Ray dashboard port |
| `NDIF_RAY_SERVE_PORT` | `8262` | Ray Serve / metrics port |
| `NDIF_RAY_TEMP_DIR` | `/tmp/ray` | Ray temporary directory |

**Controller:**

| Variable | Default | Description |
|----------|---------|-------------|
| `NDIF_CONTROLLER_IMPORT_PATH` | `src.ray.deployments.controller.controller` | Python path to Controller module |
| `NDIF_DEPLOYMENTS` | `""` | Pipe-separated model keys to deploy at startup |
| `NDIF_EXECUTION_TIMEOUT_SECONDS` | `3600` | Default execution timeout per request (per-deployment in DeploymentConfig) |
| `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` | `3600` | Min time before a model can be evicted |
| `NDIF_MODEL_CACHE_PERCENTAGE` | `0.9` | Fraction of CPU memory for warm cache |
| `NDIF_CONTROLLER_SYNC_INTERVAL_S` | `30` | Interval for node discovery polling |

**Queue:**

| Variable | Default | Description |
|----------|---------|-------------|
| `COORDINATOR_STATUS_CACHE_FREQ_S` | `120` | How long to cache cluster status |
| `COORDINATOR_PROCESSOR_REPLY_FREQ_S` | `3` | Status update frequency for queued users |

**Database (production only):**

| Variable | Description |
|----------|-------------|
| `POSTGRES_HOST` | PostgreSQL host |
| `POSTGRES_PORT` | PostgreSQL port |
| `POSTGRES_DB` | Database name |
| `POSTGRES_USER` | Database user |
| `POSTGRES_PASSWORD` | Database password |

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
│   │   │   │   └── queue/
│   │   │   │       ├── dispatcher.py   # Central request coordinator
│   │   │   │       ├── processor.py    # Per-model lifecycle manager
│   │   │   │       ├── config.py       # Queue configuration
│   │   │   │       └── util.py         # Ray client helpers
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
│   │   │   │   │   │   └── cluster/
│   │   │   │   │   │       ├── cluster.py      # Cluster state
│   │   │   │   │   │       ├── node.py         # Node resources
│   │   │   │   │   │       ├── deployment.py   # Deployment lifecycle
│   │   │   │   │   │       └── evaluator.py    # Model size evaluation
│   │   │   │   │   └── modeling/
│   │   │   │   │       ├── base.py             # ModelActor
│   │   │   │   │       └── util.py             # Model loading helpers
│   │   │   │   └── nn/
│   │   │   │       ├── backend.py              # RemoteExecutionBackend
│   │   │   │       ├── ops.py                  # StdoutRedirect
│   │   │   │       └── security/
│   │   │   │           ├── protected_environment.py  # Protector, Importer
│   │   │   │           ├── protected_objects.py      # ProtectedObject
│   │   │   │           └── whitelist.yaml            # Module/builtin whitelist
│   │   │   ├── start.sh                # Ray head startup script
│   │   │   └── start-worker.sh         # Ray worker startup script
│   │   │
│   │   └── base/
│   │       └── requirements.in         # Shared base dependencies
│   │
│   └── common/
│       ├── types.py                    # Type aliases (MODEL_KEY, API_KEY, etc.)
│       ├── logging/
│       │   └── logger.py               # Centralized logging setup
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
│       │   └── ray.py                  # Ray connection management
│       └── schema/
│           ├── request.py              # BackendRequestModel
│           ├── response.py             # BackendResponseModel
│           ├── result.py               # BackendResultModel
│           └── mixins.py               # ObjectStorageMixin, TelemetryMixin
│
├── docker/
│   ├── Dockerfile                      # Multi-purpose (api or ray)
│   └── docker-compose.yml              # Full stack orchestration
│
├── telemetry/
│   ├── grafana/
│   │   ├── dashboards/                 # Pre-configured dashboards
│   │   └── provisioning/               # Grafana data sources
│   └── prometheus/
│       └── prometheus.yml              # Scrape configuration
│
├── tests/
│   ├── test_nnsight.py                 # Integration tests
│   ├── test_security_guards.py         # Security validation
│   └── reconnection/                   # Ray failure recovery tests
│
├── pyproject.toml                      # Project metadata
├── Makefile                            # Build automation
├── .env.example                        # Default configuration
└── README.md                           # Project overview
```
