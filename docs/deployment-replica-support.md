# Deployment Replica Support

## What is a Replica?

A replica is an independent Ray actor (`ModelActor`) serving the same model. Each replica has a unique string ID (UUID) and a deterministic actor name:

```
ModelActor:<model_key>:<replica_id>
```

This naming contract is shared consistently by:
- API queue actor lookup (`queue/util.py`: `model_actor_name()`, `get_model_actor_handle()`)
- Ray deployment metadata (`cluster/deployment.py`: `Deployment.name`)
- Actor restart path (`modeling/base.py`: `BaseModelDeployment.restart()`)
- Status and schedule matching (`controller.py`, `gcal/controller.py`)

Replicas move between three deployment levels:
- **HOT** — actively loaded on GPU(s), serving requests
- **WARM** — weights held on CPU, ready to reload to GPU
- **COLD** — downloaded to disk but not loaded

## Replica State Tracking

### `Replica` class (`src/services/api/src/queue/replicas.py`)

Holds per-replica state and provides actor interaction:

- `busy: bool` — whether the replica is currently executing a request
- `worker_task: Optional[asyncio.Task]` — the replica's worker coroutine handle
- `current_request_id` / `current_request_started_at` — tracks the in-flight request
- `get_handle()` — resolves the Ray actor handle by `(model_key, replica_id)`
- `submit(method, *args)` — RPC call to the Ray actor
- `wait_until_ready()` — polls `__ray_ready__` until the actor is live, retrying on lookup failures

### `Replicas` class (`src/services/api/src/queue/replicas.py`)

Pure state container that manages a collection of `Replica` objects with aggregate bookkeeping:

- `replica_ids: list[REPLICA_ID]` — ordered list of active replica IDs
- `in_flight: int` — total requests executing across all replicas
- `_replicas: dict[REPLICA_ID, Replica]` — internal map

Key operations:
- `add(replica_id)` / `remove(replica_id)` — add or remove a replica. `remove()` cancels the replica's worker task if running
- `set_replica_ids(ids)` — atomically replace the entire replica set: removes IDs no longer present, adds new ones. Used after provisioning to sync with the controller's authoritative replica list
- `begin_request(replica_id)` / `end_request(replica_id)` — increment/decrement `in_flight` and toggle the replica's `busy` flag
- `get_state()` — snapshot of `in_flight`, `replica_count`, `replica_ids`, per-replica request IDs and timestamps

## Request Routing

Each `Processor` manages a single model's request queue and replica workers. The core design is **one shared `asyncio.Queue` per model, with one worker coroutine per replica competing on it**.

```
_replica_worker(replica_id):
    loop:
        request = await queue.get()          # whichever replica is idle first wins
        replicas.begin_request(replica_id)
        status = BUSY
        try:
            _execute_on_replica(replica_id, request)
        finally:
            replicas.end_request(replica_id)
            if in_flight == 0: status = READY
```

This provides natural load balancing without explicit routing logic — idle replicas self-select by being the first to `await queue.get()`.

### Execution on a replica

`_execute_on_replica(replica_id, request)`:
1. Sets `replica.current_request_id` and `current_request_started_at`
2. Sends a `DISPATCHED` response to the user
3. Calls `replica.submit("__call__", request)` — the actual RPC to `ModelActor.__call__`
4. In `finally`: clears the current request tracking

Error paths:
- **"Failed to look up actor"** — the actor was evicted or died. Enqueues a `(model_key, reason, replica_id)` tuple to the dispatcher's `eviction_queue` and sets processor status to `CANCELLED`
- **Other exceptions** — reported to the dispatcher's `error_queue`; the dispatcher resets the processor to `READY`

## Processor Lifecycle

```
processor_worker(provision=True):
    status = PROVISIONING
    start reply_worker()              # sends periodic status updates to queued users
    if provision: await provision()   # request deployment from Controller
    status = DEPLOYING
    await initialize()                # wait for all replicas to become ready
    status = READY
    start _replica_worker() for each replica
    await all worker tasks
```

- `provision()` calls `Controller.deploy([model_key], replica_count)` and syncs the returned replica IDs into the `Replicas` collection via `set_replica_ids()`. It also processes evictions (forwarded to the dispatcher) and handles `CANT_ACCOMMODATE` results by removing those replicas
- `initialize()` runs `_initialize_and_start_replica(replica_id)` concurrently for all replicas. Each task polls `replica.wait_until_ready()`. The processor proceeds as long as **at least one** replica succeeds; failed replicas are removed without cancelling the processor
- After initialization, `_start_replica_worker()` is called for every live replica. The processor then awaits all worker tasks via `asyncio.gather()`

### Adding/removing replicas at runtime

- `add_replica(replica_id)`: adds to `Replicas`, and if the processor is already `READY` or `BUSY`, kicks off `_initialize_and_start_replica()` as an async task — the new replica joins the worker pool without interrupting existing ones
- `remove_replica(replica_id, message)`: removes from `Replicas` (which cancels its worker task). If no replicas remain, sets status to `CANCELLED` and purges the queue

## Eviction and Failure Handling

### Pressure-driven evictions

During `Cluster.deploy()`, if a node lacks free GPU resources, it may evict existing non-dedicated replicas to make room. Evicted `(model_key, replica_id)` pairs are:
1. Returned in `results["evictions"]`
2. Forwarded by the Processor to the Dispatcher's `eviction_queue`
3. Handled by the Dispatcher: per-replica eviction calls `processor.remove_replica()`; whole-model eviction removes the entire Processor

### Warm caching on eviction

When a hot deployment is evicted from GPU:
- If sufficient CPU memory exists (tracked by `available_cpu_memory_bytes`), the deployment transitions to WARM cache
- If CPU memory is insufficient, existing cached deployments are evicted (smallest first) to make room
- The `build()` → `apply()` cycle calls `ModelActor.to_cache()` (model → CPU) and later `ModelActor.from_cache(gpu_mem_bytes_by_id)` (CPU → GPU)

### Runtime failures

- **Actor lookup failure** during execution: enqueues replica eviction, sets processor to `CANCELLED`
- **Initialization failure**: removes the failed replica, continues if other replicas remain
- **CUDA device-side assertion**: triggers actor restart via `ray.kill(actor, no_restart=False)`

## Dispatcher Integration

The Dispatcher creates a `Processor` for each model on first request and manages inter-processor communication:

**Eviction handling** (`handle_evictions()`):
- Drains `eviction_queue` tuples of `(model_key, reason, replica_id)`
- If `replica_id is None`: removes the entire processor (`remove()`)
- If `replica_id` is set: calls `processor.remove_replica(replica_id, reason)`. If that was the last replica, the processor self-cancels and is removed

**Deploy events** (`_handle_deploy_event()`):
- Updates `processor.requested_replica_count` if the processor already exists
- Does not create new processors (those are created on-demand by incoming requests)

**Evict events** (`_handle_evict_event()`):
- Per-replica: calls `processor.remove_replica()`
- Whole-model: calls `self.remove(model_key, ...)`

## Control Planes

Two control planes keep replica state synchronized:

1. **Controller RPC plane** (Ray actor methods): `deploy`, `scale`, `scale_up`, `evict` — manages cluster-side state (GPU allocation, actor lifecycle)
2. **Dispatcher event plane** (Redis stream `dispatcher:events`): `deploy`, `evict`, `kill_request`, `queue_state_request` — keeps queue-side Processor metadata aligned with cluster changes

## Known Limitations

- **No scale-down**: `scale`/`scale_up` only add replicas; removing replicas requires explicit eviction
- **No auto-scaling**: replica count is only controlled by admin operations
- **State sync lag**: scale operations go directly to Controller RPC without publishing dispatcher events; queue-side Processor replica metadata may lag
- **No smart routing**: replicas compete on a shared queue (FIFO); no request affinity or load-aware routing
