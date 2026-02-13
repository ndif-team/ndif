# Model Replica Logic Walkthrough

End-to-end walkthrough of how model replicas are identified, created, tracked, used for requests, scaled, and evicted across the API queue layer and the Ray controller layer.

## Scope

Covered components:

- API queue layer:
  - `src/services/api/src/queue/util.py`
  - `src/services/api/src/queue/replicas.py`
  - `src/services/api/src/queue/processor.py`
  - `src/services/api/src/queue/dispatcher.py`
- Ray deployment layer:
  - `src/services/ray/src/ray/deployments/controller/controller.py`
  - `src/services/ray/src/ray/deployments/controller/cluster/cluster.py`
  - `src/services/ray/src/ray/deployments/controller/cluster/node.py`
  - `src/services/ray/src/ray/deployments/controller/cluster/deployment.py`
  - `src/services/ray/src/ray/deployments/modeling/base.py`
  - `src/services/ray/src/ray/deployments/controller/gcal/controller.py`

## Replica Identity Contract

Every replica is a Ray actor named:

```
ModelActor:<model_key>:<replica_id>
```

`replica_id` is a UUID hex string (e.g., `a1b2c3d4e5f6...`). IDs are opaque — code should treat them as strings and never assume ordering or numeric values.

This naming contract is shared consistently by:

- API queue actor lookup:
  - `queue/util.py::model_actor_name()`
  - `queue/util.py::get_model_actor_handle()`
- Ray deployment metadata:
  - `cluster/deployment.py::Deployment.name`
- Actor restart path:
  - `modeling/base.py::BaseModelDeployment.restart()`
- Status/schedule matching:
  - `controller/controller.py::status()`
  - `controller/gcal/controller.py::status()`

## End-to-End Flow (Request-Driven Path)

### 1. Processor creation and initial replica set

When the dispatcher sees the first request for a `model_key`, it creates a `Processor`:

- `queue/dispatcher.py::dispatch()`

The `Processor.__init__` receives `replica_count` (default 1) and creates an empty `Replicas` collection. Replica IDs are not assigned until provisioning completes.

### 2. Provisioning via Controller

`Processor.processor_worker()` calls `provision()`:

- `queue/processor.py::provision()`
- `controller/controller.py::deploy()` → `_deploy()`

**Processor.provision()**:
1. Checks if the model has a dedicated (scheduled) deployment
2. Filters queue to remove invalid requests (non-hotswap on non-dedicated)
3. Calls `Controller.deploy([model_key], replica_count)` via Ray RPC
4. Syncs returned replica IDs into the `Replicas` collection via `set_replica_ids()`
5. Forwards eviction events to the dispatcher
6. Removes replicas that returned `CANT_ACCOMMODATE` or error statuses

**Controller._deploy()**:
1. Stores `desired_replicas[model_key] = replicas`
2. Calls `cluster.deploy(model_keys, replicas=...)` — returns `(results, change)`
3. Adjusts `desired_replicas` down for any replicas that couldn't be placed
4. Calls `apply()` if cluster state changed

### 3. Replica placement and actor creation

**Cluster.deploy()** computes target replica IDs per model:

1. Scans all nodes to collect `deployed_replica_ids` and `cached_replica_ids`
2. Calls `target_replica_ids_for()`:
   - `needed = max(0, replicas - len(deployed_replica_ids))`
   - Reuses cached IDs first (up to `needed`) — so a WARM replica gets promoted back to HOT without creating a new actor
   - Generates new UUIDs for the remainder, avoiding collisions with all existing IDs
3. Evaluates model sizes via `self.evaluator(model_key)` (loads on meta device, no GPU)
4. For each target replica, evaluates all nodes:
   - Each node returns a `Candidate` with a `CandidateLevel` (priority: `DEPLOYED=0` < `CACHED_AND_FREE=1` < `FREE=2` < `CACHED_AND_FULL=3` < `FULL=4` < `CANT_ACCOMMODATE=5`)
   - Among tied candidates, one is chosen randomly
5. Calls `node.deploy()` for placed replicas to update resource accounting

**Node.deploy()** and **Node.evaluate()** decide placement per replica:
- Full-GPU placement, single-GPU fractional placement, or multi-GPU placement
- Optional eviction of non-dedicated replicas to make room
- See `gpu-fraction-deployment.md` for the full decision tree

New hot deployments are materialized into actors during **Controller.apply()**:

- `build()` diffs `self.state` against current cluster nodes to produce a `DeploymentDelta`:

| Category | Condition | Action |
|----------|-----------|--------|
| `deployments_to_create` | Not in `state`, now in node's `deployments` | Create new `ModelActor` |
| `deployments_from_cache` | Was WARM in `state`, now in node's `deployments` | Call `ModelActor.from_cache(gpu_mem_bytes_by_id)` |
| `deployments_to_cache` | Was HOT in `state`, now in node's `cache` | Call `ModelActor.to_cache()` |
| `deployments_to_delete` | In `state` but absent from new cluster view | Kill the actor |

- Cache operations run synchronously (must complete before `from_cache` can reuse freed GPU)
- Create and from-cache are monitored via async tasks; failures trigger `deployment.delete()` and `_remove_deployment_from_state()`

`Deployment.create()` spawns the actor:
- `ModelActor.options(name="ModelActor:<model_key>:<replica_id>", ...).remote(**deployment_args)`
- Uses `RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES=1` so the actor sees all GPUs; targeting is done via `max_memory`

### 4. Readiness gate

After provisioning:

- `Processor.initialize()` starts `_initialize_and_start_replica(replica_id)` tasks for all `replica_ids`
- Each task calls `replica.wait_until_ready()`, which polls `__ray_ready__` on the Ray actor
- The processor proceeds as long as **at least one** replica succeeds
- Failed replicas are removed via `remove_replica()` without cancelling the processor

### 5. Request execution across replicas

For each replica, the processor starts one worker coroutine via `_start_replica_worker()`:

```
_replica_worker(replica_id):
    loop:
        request = await queue.get()          # shared queue — whichever replica is idle first wins
        replicas.begin_request(replica_id)
        status = BUSY
        try:
            _execute_on_replica(replica_id, request)
        finally:
            replicas.end_request(replica_id)
            if in_flight == 0: status = READY
```

**_execute_on_replica()**:
1. Sets `replica.current_request_id` and `current_request_started_at`
2. Sends a `DISPATCHED` response to the user
3. Resolves actor handle by exact `(model_key, replica_id)` and calls `ModelActor.__call__`
4. In `finally`: clears the current request tracking

Error handling:
- **"Failed to look up actor"**: enqueues replica eviction to dispatcher, sets `CANCELLED`
- **Other errors**: reported to dispatcher's `error_queue`; dispatcher resets processor to `READY`

## Runtime Behavior Inside a Replica (ModelActor)

`ModelActor` is a Ray remote class in `modeling/base.py` that inherits `BaseModelDeployment`.

Important replica behaviors:

- **`__call__`**: rejects work if replica is in cached mode (`self.cached`); runs pre → execute → post pipeline; supports timeout and cancellation (kill switch)
- **`cancel()`**: triggers in-flight cancellation
- **`to_cache()`**: cancels current work, removes accelerate hooks, moves model to CPU, resets memory fraction to 1.0, marks replica cached
- **`from_cache(gpu_mem_bytes_by_id)`**: updates GPU targets, sets memory fraction caps, re-dispatches model via `accelerate.dispatch_model`
- **`restart()`**: kills the specific replica actor with `no_restart=False`

## Eviction and Failure Paths

### Planned or pressure-driven evictions

`Cluster.deploy()` may evict existing non-dedicated replicas to make space; it returns `results["evictions"]` as `(model_key, replica_id)` pairs.

`Processor.provision()` forwards those to the dispatcher's eviction queue.

Dispatcher handling:
- If `replica_id is None`: remove entire processor
- Else: call `processor.remove_replica(replica_id, reason)`

### Explicit evictions

Controller eviction API supports:
- Whole-model eviction (`model_keys=[...]`)
- Single-replica eviction (`replica_keys=[(model_key, replica_id)]`)

Controller side:
- `controller.evict()` updates `desired_replicas`:
  - If evicting specific replicas: decrements the desired count per model
  - If evicting whole models: sets desired replicas to `0`
- Calls `cluster.evict(...)` and, if state changed, calls `apply()`

Node side:
- Returns GPU memory to resources
- If sufficient CPU memory: moves deployment to warm cache
- If not: evicts cached deployments (smallest first) to make room

### Runtime failures

- **Actor lookup failure during execute**: enqueues replica eviction, calls processor `CANCELLED`
- **Initialization failure**: removes failed replica, decreases desired replicas
- **CUDA device-side assertion**: triggers actor restart via `ray.kill(actor, no_restart=False)`

## Data Structures

### `Deployment` (`cluster/deployment.py`)

Per-replica deployment record:
- `model_key`, `replica_id`, `node_id`
- `deployment_level`: HOT / WARM / COLD
- `gpu_mem_bytes_by_id: Dict[int, int]` — per-GPU memory allocation
- `gpu_memory_fraction: float | None` — fraction passed to `torch.cuda.set_per_process_memory_fraction`
- `size_bytes` — model size for CPU cache accounting
- `dedicated: bool` — dedicated deployments are not evictable by pressure

### `Node` (`cluster/node.py`)

Per-worker-node state:
- `deployments: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]]` — two-level map of hot deployments
- `cache: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]]` — two-level map of warm-cached deployments
- `resources: Resources` — GPU/CPU memory accounting

### `Replicas` / `Replica` (`queue/replicas.py`)

API-side replica state:
- `Replica`: per-replica busy flag, worker task, current request tracking, actor handle
- `Replicas`: collection with `in_flight` counter, atomic `set_replica_ids()`, `begin_request`/`end_request`

## Configuration That Affects Replicas

From `queue/config.py`:
- `COORDINATOR_PROCESSOR_REPLY_FREQ_S` — progress update frequency while provisioning/deploying

From node resource logic:
- `min_available_gpu_fraction` in `Resources` (default 0.3) — threshold to consider a GPU available
- `gpu_fraction_factor` (3.0) and `fraction_largest_possible` (0.8) in `Node.evaluate()` — control fractional vs full allocation

Request-driven processors default to a single replica and begin serving once that replica is ready.

## File-Level Map

Replica identity and lookup:
- `src/services/api/src/queue/util.py`
- `src/services/ray/src/ray/deployments/controller/cluster/deployment.py`

Queue-side lifecycle and request routing:
- `src/services/api/src/queue/replicas.py`
- `src/services/api/src/queue/processor.py`
- `src/services/api/src/queue/dispatcher.py`
- `src/services/api/src/queue/config.py`

Controller/cluster placement and eviction:
- `src/services/ray/src/ray/deployments/controller/controller.py`
- `src/services/ray/src/ray/deployments/controller/cluster/cluster.py`
- `src/services/ray/src/ray/deployments/controller/cluster/node.py`

Replica runtime implementation:
- `src/services/ray/src/ray/deployments/modeling/base.py`
