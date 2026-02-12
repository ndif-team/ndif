# Model Replica Logic Walkthrough

This document explains how model replicas are identified, created, tracked, used for requests, scaled, and evicted across the API queue layer and the Ray controller layer.

## Scope

Covered components:

- API queue layer:
  - `src/services/api/src/queue/util.py`
  - `src/services/api/src/queue/processor.py`
  - `src/services/api/src/queue/dispatcher.py`
  - `src/services/api/src/queue/config.py`
- Ray deployment layer:
  - `src/services/ray/src/ray/deployments/controller/controller.py`
  - `src/services/ray/src/ray/deployments/controller/cluster/cluster.py`
  - `src/services/ray/src/ray/deployments/controller/cluster/node.py`
  - `src/services/ray/src/ray/deployments/controller/cluster/deployment.py`
  - `src/services/ray/src/ray/deployments/modeling/base.py`
  - `src/services/ray/src/ray/deployments/controller/gcal/controller.py`
- External control entrypoints:
  - `cli/commands/deploy.py`
  - `cli/commands/evict.py`
  - `cli/commands/scale.py`
  - `cli/lib/util.py`

## Replica Identity Contract

Every replica is a Ray actor named:

`ModelActor:<model_key>:<replica_id>`

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

Replica initialization in `Processor.__init__`:

- `replica_count` is:
  - explicit argument if provided, otherwise
  - defaults to `1`
- `replica_ids = [0..replica_count-1]`
- per-replica execution tracking dicts are initialized

### 2. Provisioning via Controller

`Processor.processor_worker()` calls `provision()` (unless created from an external deploy event with `provision=False`).

Provisioning calls:

- `Controller.deploy([model_key], replicas=replica_count)`

in:

- `queue/processor.py::provision()`
- `controller/controller.py::deploy()` -> `_deploy()`

Controller `_deploy()`:

- stores desired replicas in `self.desired_replicas[model_key]`
- calls `cluster.deploy(model_keys, replicas=...)`
- calls `apply()` if cluster state changed

### 3. Replica placement and actor creation

`Cluster.deploy()` computes target replica IDs per model:

- if none exist: `[0..replicas-1]`
- if some exist and target is larger: appends new IDs after max existing ID
- if existing count already >= target: keeps existing IDs (no downscale) [TODO: just remove last few replicas for now]

`Node.evaluate()` and `Node.deploy()` decide placement per replica:

- full-GPU placement or single-GPU memory-fraction placement
- optional eviction of non-dedicated replicas to make room

New hot deployments are materialized into actors during `Controller.apply()`:

- `Deployment.create()` -> `ModelActor.options(...).remote(...)`
- actor name uses the replica identity contract above

### 4. Readiness gate

After provisioning:

- `Processor.initialize()` starts `initialize_replica(replica_id)` tasks for all `replica_ids`
- each task loops until actor lookup + `__ray_ready__` succeeds
- the processor waits for all initialization tasks to finish, then proceeds if at least one replica remains

### 5. Request execution across replicas

For each replica, processor starts one worker coroutine:

- `queue/processor.py::_start_replica_worker()`
- `queue/processor.py::replica_worker()`

Each replica worker:

- pulls from the shared model queue
- updates per-processor execution bookkeeping:
  - increments `in_flight`
  - adds `replica_id` to `busy_replicas`
  - sets `status=BUSY` and calls `reply()` so queued users see progress
- calls `execute(request, replica_id)`

Execution:

- actor handle resolved by exact `(model_key, replica_id)`
- request sent to `ModelActor.__call__`
- per-replica `current_request_ids[replica_id]` and `current_request_started_ats[replica_id]` are set before dispatch and cleared in a `finally` block
- on success, the replica worker decrements `in_flight`, removes `replica_id` from `busy_replicas`, and sets `status=READY` when `in_flight==0`
- on error:
  - if the error looks like actor eviction (`Failed to look up actor...`), the processor enqueues an eviction event for that replica, decreases desired replicas via controller `evict`, and sets `status=CANCELLED`
  - otherwise, it reports the exception to the dispatcher's `error_queue`; the dispatcher later logs it and (if no reconnect) resets the processor back to `READY`

## Runtime Behavior Inside a Replica (ModelActor)

`ModelActor` is a Ray remote class in `modeling/base.py` that inherits `BaseModelDeployment`.

Important replica behaviors:

- `__call__`:
  - rejects work if replica is in cached mode (`self.cached`)
  - runs pre -> execute -> post pipeline
  - supports timeout and cancellation (kill switch)
- `cancel()`:
  - triggers in-flight cancellation
- `to_cache()`:
  - cancels current work, moves model to CPU, marks replica cached
- `from_cache(gpu_mem_bytes_by_id)`:
  - moves cached model back to GPU
- `restart()`:
  - kills the specific replica actor with `no_restart=False`

## Eviction and Failure Paths

### Planned or pressure-driven evictions

`Cluster.deploy()` may evict existing replicas to make space; it returns:

- `results["evictions"]` as `(model_key, replica_id)` pairs

`Processor.provision()` forwards those to dispatcher's eviction queue.

Dispatcher handling:

- if `replica_id is None`: remove entire processor
- else: call `processor.remove_replica(replica_id, reason)`

### Explicit evictions

Controller eviction API supports:

- whole-model eviction (`model_keys=[...]`)
- single-replica eviction (`replica_keys=[(model_key, replica_id)]`)

Controller side:

- `controller/controller.py::evict()` updates `desired_replicas`:
  - if evicting specific replicas, it decrements the desired count for that model
  - if evicting whole models, it sets desired replicas to `0`
- it then calls `cluster.evict(...)` and, if the cluster reports `change=True`, it calls `controller.apply()` to materialize the new state in Ray actors.

Cluster side:

- `cluster.evict()` updates node resources and optionally moves to warm cache

Apply / state materialization:

`controller.apply()` diffs controller-tracked state against the latest `cluster.nodes` view and triggers the actual actor-side operations:

- HOT -> WARM (evict with `cache=True`): `build()` classifies the replica into `deployments_to_cache`, and `apply()` calls `Deployment.cache()` which invokes `ModelActor.to_cache()` (move weights to CPU, mark cached).
- WARM -> HOT (redeploy from cache): `build()` classifies into `deployments_from_cache`, and `apply()` calls `Deployment.from_cache()` which invokes `ModelActor.from_cache(gpu_mem_bytes_by_id)` (move back to GPU).
- Removed replicas (no longer present in deployments or cache): `build()` classifies into `deployments_to_delete`, and `apply()` calls `Deployment.delete()` (kills the actor).

Queue side sync:

- CLI `deploy` / `evict` emits redis events to `dispatcher:events`
- dispatcher events worker updates processor metadata accordingly

### Runtime failures

Key processor failure handling:

- actor lookup failure during execute:
  - enqueue replica eviction
  - call `_decrease_desired_replica(replica_id)` (controller `evict`)
  - set processor status to `CANCELLED`
- initialization failure:
  - enqueue replica eviction
  - decrease desired
  - remove failed replica metadata

## Control Planes

There are two control planes for replicas:

1. Controller RPC plane (Ray actor methods)
- `deploy`
- `scale`
- `scale_up`
- `evict`

2. Dispatcher event plane (Redis stream `dispatcher:events`)
- `deploy`
- `evict`
- `kill_request`
- `queue_state_request`

The event plane keeps queue-side `Processor` metadata aligned with cluster changes for
operations that publish those events.

## Configuration That Affects Replicas

From `queue/config.py`:

- `COORDINATOR_PROCESSOR_REPLY_FREQ_S`
  - progress update frequency while provisioning/deploying

Request-driven processors default to a single replica and begin serving once
that replica is ready.

From node resource logic:

- `min_available_gpu_fraction` in `Resources` determines when a GPU remains in "available" set
- `gpu_memory_coefficient` and `gpu_memory_buffer_bytes` affect fraction-based placement

## Current Behavior and Operational Notes

- `scale`/`scale_up` are scale-up only in practice:
  - if target replicas <= current replicas, no downscale occurs
- Replica IDs are monotonic per model:
  - new replicas are assigned starting at `max(existing)+1`
- Dispatcher deploy event handler only adds replicas for existing processors:
  - it does not remove extras when lower replica count is requested
- `scale`/`scale_up` CLI paths call controller RPC directly and do not publish dispatcher deploy events:
  - queue-side processor replica metadata may stay stale until another event/request path updates it
- `desired_replicas` is tracked in controller, but there is no separate reconciliation loop enforcing it
- Queue throughput for a model grows with count of replica workers, since each replica has one worker consuming the shared request queue

## File-Level Map

Replica identity and lookup:

- `src/services/api/src/queue/util.py`
- `src/services/ray/src/ray/deployments/controller/cluster/deployment.py`

Queue-side lifecycle and request routing:

- `src/services/api/src/queue/processor.py`
- `src/services/api/src/queue/dispatcher.py`
- `src/services/api/src/queue/config.py`

Controller/cluster placement and eviction:

- `src/services/ray/src/ray/deployments/controller/controller.py`
- `src/services/ray/src/ray/deployments/controller/cluster/cluster.py`
- `src/services/ray/src/ray/deployments/controller/cluster/node.py`
- `src/services/ray/src/ray/deployments/controller/cluster/evaluator.py`

Replica runtime implementation:

- `src/services/ray/src/ray/deployments/modeling/base.py`

External ops and dispatcher notifications:

- `cli/commands/deploy.py`
- `cli/commands/evict.py`
- `cli/commands/scale.py`
- `cli/lib/util.py`
