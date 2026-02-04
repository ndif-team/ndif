# NDIF

NDIF (National Deep Inference Fabric) is a server that executes nnsight intervention requests remotely.

## Architecture overview

NDIF has two modes of operation: **production** (full distributed stack) and **local** (single-model, no infrastructure).

### Production mode (`ndif start`)

Multi-model distributed server using Redis, Ray, MinIO, and SocketIO with Redis manager.

```
nnsight client
    │
    ├─ SocketIO ws://host/ws/socket.io  (status updates, blocking responses)
    ├─ POST /request                     (submit intervention graph)
    └─ GET <presigned S3 URL>            (download result)
    │
FastAPI API (src/services/api/)
    │
    ├─ Redis queue
    ├─ SocketIO (AsyncRedisManager for multi-server)
    └─ MinIO (S3) object store
    │
Ray cluster (src/services/ray/)
    │
    ├─ Dispatcher: routes requests from Redis queue to model actors
    ├─ Controller: manages model deployment lifecycle
    └─ ModelActor (per model): loads model, executes interventions
        │
        ├─ Protector (RestrictedPython sandbox)
        ├─ RemoteExecutionBackend (Globals management + tracer.execute)
        └─ ObjectStorageMixin (result → S3 → presigned URL)
```

### Local mode (`ndif local start <checkpoint>`)

Single-model server with no infrastructure dependencies. Located in `src/local/`.

```
nnsight client
    │
    ├─ SocketIO ws://host/ws/socket.io  (same path as production)
    ├─ POST /request                     (same endpoint)
    └─ GET /results/{id}                 (filesystem-served result)
    │
FastAPI + in-memory SocketIO (src/local/server.py)
    │
    ├─ asyncio.Queue (replaces Redis)
    ├─ SocketIO (AsyncServer, no Redis manager)
    └─ Filesystem (replaces MinIO)
    │
Background worker (single asyncio task)
    │
    ├─ No sandbox (RemoteExecutionBackend with nullcontext(), trusts user code)
    └─ save_result() with TensorStoragePickler (result → disk → HTTP URL)
```

## Local mode design details

### Request flow

The local server is fully compatible with the nnsight client's `remote=True` interface:

1. Client connects SocketIO at `ws://host/ws/socket.io`
2. Client sends `POST /request` with headers:
   - `nnsight-model-key`: model identifier (validated against loaded model)
   - `ndif-session_id`: SocketIO session ID for response routing
   - `nnsight-compress`: whether request body is zstd-compressed (`"True"` / `"False"`)
3. Server validates model key, queues request, returns `RECEIVED` ResponseModel
4. Background worker picks from `asyncio.Queue`:
   - Emits `RUNNING` status via SocketIO
   - Deserializes with `RequestModel.deserialize(body, persistent_objects, compress)`
   - Executes with `RemoteExecutionBackend` + `nullcontext()` (Globals enter/exit + `tracer.execute(fn)`, no sandbox)
   - Saves result to disk with `TensorStoragePickler` + optional zstd compression
   - Emits `COMPLETED` with `(result_url, result_size)` tuple
5. Client downloads result via `GET /results/{id}`
6. Client decompresses (if compress=True) and `torch.load`s the result

### Key nnsight interfaces used

- `RemoteableMixin.from_model_key(key, device_map="auto")` — loads model from HF checkpoint
- `model._remoteable_persistent_objects()` — returns `{"Interleaver": ..., "Module:path": ...}` dict needed for request deserialization
- `RequestModel.deserialize(bytes, persistent_objects, compress)` — deserializes intervention graph
- `ResponseModel.pickle()` / `ResponseModel.unpickle()` — uses `torch.save`/`torch.load`, NOT standard pickle
- `tracer.execute(fn)` — runs the intervention function, returns dict of saved values

### Model key format

```
nnsight.modeling.language.LanguageModel:{"repo_id": "openai-community/gpt2", "revision": null}
```

The import path prefix identifies the class, the JSON suffix identifies the specific model. The `"revision": "main"` → `"revision": null` normalization is applied to incoming requests (matching production behavior at `src/common/schema/request.py:86`).

### Port behavior

- Default: start at 8289, auto-increment until finding an available port
- Explicit `--port`: fail immediately if port is in use

### Differences from production

| Aspect | Production | Local |
|---|---|---|
| Queue | Redis `lpush`/`brpop` | `asyncio.Queue` |
| SocketIO | `AsyncRedisManager` (multi-server) | `AsyncServer` (in-memory) |
| Result storage | MinIO S3 + presigned URLs | Filesystem + `GET /results/{id}` |
| Sandboxing | `RestrictedPython` Protector | None (trusts user code) |
| Model routing | Dispatcher routes by model key | Single model, validates key at `/request` |
| Model count | Multiple models via Ray actors | One model per server instance |
| Execution | `RemoteExecutionBackend` (with Protector) | `RemoteExecutionBackend` (with `nullcontext()`, no Protector) |

## CLI structure

```
ndif
├── start          # Start full NDIF (Redis, Ray, API, etc.)
├── stop           # Stop full NDIF
├── restart        # Restart model deployment
├── deploy         # Deploy model(s) to Ray
├── evict          # Remove model deployment(s)
├── status         # View cluster status
├── queue          # View queue status
├── logs           # View service logs
├── kill           # Cancel a request
├── info           # Show session info
├── env            # Show cluster environment
└── local
    └── start      # Start local single-model server

```

## Dependency structure

`pyproject.toml` splits dependencies:

- **Base** (`pip install ndif`): click, fastapi, python-socketio, uvicorn, zstandard, nnsight, torch, pyyaml
- **Server** (`pip install ndif[server]`): adds ray, redis, boto3, RestrictedPython, gunicorn, eventlet, etc.

**Note**: Local mode imports from production modules (`src.common.schema`, `src.services.ray...backend`, `cli.lib.util`), so it requires `[server]` dependencies (ray, boto3) to be installed.
