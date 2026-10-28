---
title: Model Actor
one_liner: The Ray actor that owns a model's GPU-resident weights and runs one request against them — loading, the run() template, results, timeouts, and metrics.
tags: [internals, dev, ray, telemetry]
related: [docs/developing/adding-a-model-actor.md, docs/developing/sandbox-internals.md, docs/developing/controller-internals.md, docs/developing/queue-internals.md, docs/concepts/request-lifecycle.md, docs/concepts/status-and-results.md, docs/concepts/sandbox-execution.md, docs/concepts/deployments-and-eviction.md, docs/developing/nnsight-integration.md, docs/reference/env-vars.md]
sources: [src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/deployments/modeling/util.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/common/schema/request.py, src/ndif/common/providers/objectstore.py, src/ndif/common/metrics.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/auth.py, src/ndif/services/ray/sandbox/model.py]
---

# Model Actor

## What this covers

`BaseModelDeployment` (`src/ndif/services/ray/deployments/modeling/base.py`) is the class that holds a model: one instance per
replica, each a detached Ray actor in its own worker process, created by the
controller and called by the queue. This page covers its constructor args, weight
loading and GPU placement, HOT↔WARM caching, the per-request `run()` path, statuses
and results, timeouts, error handling, metrics, and lifecycle. Three facts shape the
design:

1. **The actor outlives requests.** Weights stay resident for hours, so a request's
   leftovers — linecache entries, nnsight's compiled-block memo, CUDA activations —
   would leak into the next user's run if they weren't scrubbed.
2. **The code it runs is arbitrary user Python.** It must be timeboxed, cancellable
   mid-flight, and when it raises, the user's own traceback has to come back to them.
3. **The base actor is the *trusted* execution path** — its `execute` runs the block
   in this process, next to the weights. Untrusted requests are diverted to a
   separate runner process; see [below](#trusted-and-untrusted-requests).

`run()` is a **template method** built around those facts: a subclass overrides a few
hooks and inherits the rest — recipe in [Adding a Model Actor](adding-a-model-actor.md).

## Construction

`Deployment.create` (`src/ndif/services/ray/deployments/controller/cluster/deployment.py`) resolves the actor class and calls
`actor_class.options(name=..., resources={f"node:{node}": 0.01}, namespace="NDIF",
lifetime="detached", runtime_env=...).remote(**deployment_args.model_dump())`, where
`deployment_args` is a `BaseModelDeploymentArgs` (`src/ndif/services/ray/deployments/modeling/base.py`).

| Field | Default | Where it comes from |
|---|---|---|
| `model_key` | required | The nnsight model key (`"{import.path}:{json}"`) this deployment serves |
| `execution_timeout` | `None` | `DeploymentConfig.execution_timeout_seconds`, else `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS`, **unset by default**, so no cap at all (`_ControllerActor.apply`) |
| `gpu_mem_bytes_by_id` | `{}` | Injected by `Deployment.create` from the placement the cluster chose |
| `dtype` | `"bfloat16"` | `DeploymentConfig.dtype`, else `NDIF_DEFAULT_DTYPE` |

Only these four fields cross the boundary — a constructor parameter that isn't a
field here can never be set by the controller. `BaseModelDeployment.__init__` binds
all four by name; anything else lands in `self.kwargs`, forwarded verbatim into
`from_model_key` at load time. `trust_remote_code` is not an argument: `from_model_key`
reads it off the model key
([Models and Deployment](../operating/models-and-deployment.md#trust_remote_code-in-the-model-key)).
`dtype` is kept twice: `dtype_name` (what the loader is handed — a quantization name
like `nf4` is not a `torch.dtype`) and `dtype`, the resolved compute dtype that user
execution autocasts to.

`__init__` connects Loki and Influx first — each Ray actor is its own process, so
sinks installed on the driver don't exist here. Their config arrives via the actor's
`runtime_env`, filled from the controller's environment (`_provider_runtime_env` in
`src/ndif/services/ray/deployments/controller/cluster/deployment.py`) along with `NDIF_SERVICE=model`,
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, and
`RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES=1` — that last one matters, because the
actor sees *every* GPU on the node and targets the assigned ones itself. The actor
registers as `{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace
(`Deployment.name`), which is what the queue looks up (`get_model_actor_handle` in
`src/ndif/common/providers/ray.py`).

## Loading the model

`BaseModelDeployment.load_from_disk` runs in the constructor: the actor isn't
`__ray_ready__` until the weights are on the GPUs.

```python
set_default_gpu(self.gpu_mem_bytes_by_id)      # before any CUDA call
set_process_limits(self.gpu_mem_bytes_by_id)   # per-process allocator cap
gpu_ids = sorted(self.gpu_mem_bytes_by_id)
if len(gpu_ids) == 1:
    placement = {"device": gpu_ids[0]}
else:
    placement = {"device_map": "balanced",
                 "max_memory": build_max_memory(self.gpu_mem_bytes_by_id)}
model = HuggingFaceModel.from_model_key(
    self.model_key, dispatch=True, dtype=self.dtype_name, **placement, **self.kwargs)
```

- `set_default_gpu` (`src/ndif/services/ray/deployments/modeling/util.py`) pins the default CUDA device to the first assigned GPU:
  the ~400 MiB CUDA context is created on the *current* device, so without it it
  lands on `cuda:0`, a card this replica may not own. `set_process_limits` caps the
  allocator per device at the granted budget; `build_max_memory` clamps each budget
  to physical memory so accelerate can't plan an impossible fit.
- One card is a separate call: the model loads through `transformers.pipeline`,
  which puts a single-device map on `cuda:0` whatever the map says, so only an
  explicit `device=` lands it on the assigned card.
- `from_model_key` is nnsight's `Remotable` classmethod: it splits the class import
  path off the key and delegates to that wrapper's `_remoteable_from_model_key`. For
  a HuggingFace model the rest of the key is `{"repo_id": ..., "revision": ...,
  "task": ..., "trust_remote_code": true}` (the last only when set), so **the
  revision, the pipeline task and `trust_remote_code` are part of the model key**,
  not separate args. `dispatch=True` skips nnsight's meta-device phase.
- After a `torch.cuda.synchronize()` (accelerate's copies are async),
  `verify_device_placement` walks every parameter and buffer and raises if one is
  still on `meta`/`cpu`, sits on an unassigned card, or if an assigned card holds
  nothing. Grads are disabled and `ModelLoadTimeMetric` is emitted with
  `load_type="initial"`.
- The constructor then runs `gc.collect()` and `gc.freeze()`, moving the loaded
  model's objects into the permanent generation so the full `gc.collect` in
  `cleanup()` skips them on every request. `from_cache` does not refreeze: a
  request preempted by `to_cache` can still have a thread running then.

> **Gotcha:** `dtype` must match what the controller's evaluator sized this model
> with, or the GPU accounting that placed the replica is wrong. Hence the controller
> pins `config.dtype` before evaluating
> (`src/ndif/services/ray/deployments/controller/controller.py` `_ControllerActor._deploy`).

PEFT adapters are **not** a load-time concern: the base model is named by the model
key, and an adapter travels per request in `request.env`, applied by
`_remoteable_set_env` just before execution. nnsight's `TransformersModel` rewraps
the module only when the requested adapter differs from the current one, so a repeat
request pays nothing.

## HOT ↔ WARM

The controller moves a replica between levels through `Deployment.cache` and
`Deployment.from_cache`:

| Call | Method | What happens |
|---|---|---|
| HOT→WARM | `BaseModelDeployment.to_cache` | `cancel("preempted")` any in-flight run, `_module.to("cpu")`, `reset_process_limits`, `synchronize` + `gc.collect` + `empty_cache`, `self.cached = True` |
| WARM→HOT | `BaseModelDeployment.from_cache` | Re-apply limits for the (possibly different) GPUs, compute a balanced `device_map`, `redispatch` onto it, verify, `self.cached = False`, emit `ModelLoadTimeMetric(load_type="from_cache")` |

`redispatch` is not a bare `dispatch_model`. It strips the previous dispatch's
accelerate hooks first, or the new ones stack on top of them, and it keeps every
module's instance `forward` across the re-dispatch: nnsight's controller lives in
that slot, and both the hook removal and the new dispatch overwrite it, which left
a restored multi-GPU replica uninstrumented and every trace on it raising
`OutOfOrderError` (#339). While `cached` is true, `run()`
raises `CachedActorError` *before* its try block so it reaches the queue as an
eviction rather than a user-facing ERROR; `Replica.dispatch` treats it like a dead
actor — drop the replica, re-queue at the front. An actor class that sets
`CACHEABLE = False` is never parked; the controller removes it outright instead.

## Trusted and untrusted requests

`BackendRequestModel.trusted` (`src/ndif/common/schema/request.py`) is whether
the request's tags, stamped at ingress from the API key, include `trusted`;
**with auth off — no `NDIF_POSTGRES_URL` — the tags default to `{"trusted"}` but
a client-supplied `tags: []` is honored** (`src/ndif/services/api/auth.py`
`validate_request`). The flag decides where the block runs:

- **Trusted** → `BaseModelDeployment.execute`, in the actor process, in the same
  address space as the weights. That is the path documented below.
- **Untrusted** → `SandboxModelDeployment.execute`
  (`src/ndif/services/ray/sandbox/model.py`) ships the payload to a runner
  **process** and drives the model over a socket. This is process separation, not a
  security boundary: the runner is an ordinary OS process with no hardening.
  Everything in `run()` around `execute` is the same code either way.

## One request, inside the actor

The queue's replica worker calls `handle.run.remote(request)` and awaits it
(`Replica.dispatch` in `src/ndif/services/api/queue/replica.py`). The request carries
`payload` (the serialized tracer blob), `compress`, `env`, `tags`, and the
identity fields (`id`, `api_key`, `email`, `session_id`) used for responses and
telemetry.

```mermaid
sequenceDiagram
    participant Q as queue Replica
    participant R as run event loop
    participant T as execute thread
    participant M as model on GPU
    participant S as object store
    participant C as client channel

    Q->>R: run request
    R->>C: respond RUNNING
    R->>R: gpu_baselines; snapshot linecache/SOURCES/BLOCKS
    R->>M: _remoteable_set_env — PEFT swap
    R->>T: run_on_own_thread execute, raced vs timeout and kill_switch
    T->>T: RequestModel.deserialize payload
    T->>M: tracer.execute — interleaved forward pass
    T-->>C: respond LOG per printed line
    T-->>R: torch.save blob, deserialize_ms
    R->>R: prepare_result — compress, meter
    alt blob over the socket cap, or no session_id
        R->>S: put request-id.pt, then presign a GET url
    end
    R->>R: finally restore snapshots, cleanup
    R->>C: respond COMPLETED with the blob or the url
```

### The race

Execution runs on a thread created for the request so the event loop can race
it (`BaseModelDeployment.run`):

```python
job = run_on_own_thread(lambda: self.execute(request), name=f"execute-{request.id}")
kill = asyncio.create_task(self.kill_switch.wait())
with self.execution_scope(request):
    done, pending = await asyncio.wait({job, kill},
        timeout=self.execution_timeout, return_when=asyncio.FIRST_COMPLETED)
```

| Winner | Response | `report()` status |
|---|---|---|
| `job` | continue to upload → `COMPLETED` | `completed` |
| `kill`, reason `preempted` (`to_cache` parking the replica) | none to the user: raises `CachedActorError`, so the queue re-queues the request at the front of its group | `preempted` |
| `kill`, any other reason (a deliberate `cancel()`: `ndif kill`, via `Replica.cancel`) | `ERROR` "Your job was cancelled or preempted by the server." | `cancelled` |
| neither (timeout) | `ERROR` "Your job exceeded the execution timeout of `{n}`s." | `timeout` |

The reason travels on `kill_reason`, set by `cancel(reason)`; only
`KILL_REASON_PREEMPTED` re-queues. The timeout covers only the raced wait —
deserialize plus execute; the env swap before it and the upload after it sit
outside. On cancel, preemption or timeout `interrupt()` calls `kill_thread`
(`src/ndif/services/ray/deployments/modeling/util.py`), injecting `SystemExit` via `PyThreadState_SetAsyncExc`, which fires only at
a Python bytecode boundary: it does not interrupt a CUDA kernel or native op already
in flight, and the response and `cleanup()` do not wait for the thread to stop. The
sandbox actor stops the runner process first, then does the same. The thread is
created for the request, so an injection that lands late reaches only this
request or nothing (`run_on_own_thread` in
`src/ndif/services/ray/deployments/modeling/util.py` says why).

### The worker-thread body

`BaseModelDeployment.execute` is the hook a subclass replaces to run the block
elsewhere:

```python
self.execution_ident = threading.current_thread().ident
tracer, deserialize_ms = prepare_traced_block(
    self.model, request.payload, request.compress,
    deserialize=BackendRequestModel.deserialize)
self.commit()
saved = execute_traced_block(tracer, self.dtype)
buffer = io.BytesIO()
torch.save(saved, buffer, pickle_module=cpu_pickle_module())
return buffer.getvalue(), deserialize_ms
```

The two halves live in `src/ndif/services/ray/deployments/modeling/nns.py`, split so the tensor-parallel actor can reuse them — its
shards prove they can *build* a block before rank 0 commits to a forward pass.

- `prepare_traced_block` recomputes `model._remoteable_persistent_objects()` *after*
  the env swap, so a PEFT rebind is reflected in the `Module:<path>` ids the payload
  resolves against, then calls `BackendRequestModel.deserialize` — which recompiles
  the block from its shipped **source**, not bytecode, registers it in `linecache` so
  a traceback can show the user's line even though their file doesn't exist here,
  and classifies every failure into a caller-facing `RequestError`.
- `BaseModelDeployment.commit` is the seam between *can this request run* and
  *running it* — empty in the base actor, where the tensor-parallel one releases its
  other ranks.
- `execute_traced_block` seeds the block, enters the `request_dtype` autocast region
  (thread-local, which is why it lives here and not in `run()`), and brackets
  `tracer.execute` in `inc()`/`dec()` the way the client's `Tracer.__exit__` does, so
  `nnsight.save()` records into this thread's save set. Saves are matched **by
  identity** against frame locals — an unbound `.save()` has no name and never
  appears in the result.
- `cpu_pickle_module()` (`src/ndif/services/ray/deployments/modeling/util.py`) is a `pickle` stand-in whose `Pickler` relocates
  CUDA tensors to CPU first, yielding materially smaller blobs.

### Statuses, logs, and errors

Everything the client sees goes through `BackendRequestModel.respond`
(`src/ndif/common/schema/request.py`; delivery is in
[The Redis Layer](redis-layer.md#response-publishing)). The actor emits `RUNNING` on
entry, zero or more `LOG`s, and exactly one `COMPLETED`/`ERROR` — except on
preemption, where it emits nothing more and the request re-runs elsewhere. Every
`COMPLETED` and `ERROR` carries a `meta` dict built by `request_meta` (`src/ndif/services/ray/deployments/modeling/util.py`): the
run's wall-clock `runtime` in seconds, per-GPU memory this request drove above the
resident weights (`max_mem_by_gpu`, `max_mem_pct_by_gpu`, `max_memory_usage`), and,
when the CUDA allocator refused an allocation, `alloc_shortfall_by_gpu`.

`LOG` comes from `BaseModelDeployment.execution_scope`, which redirects both
`sys.stdout` and `sys.stderr` to separate `LogStream`s (`src/ndif/services/ray/deployments/modeling/util.py`), each emitting one
`LOG` per complete line — so a `warnings.warn` in the user's block reaches the user.
The redirect is installed on the event-loop thread, since both streams are
process-global and the restore must not run on the thread `interrupt()` kills. The
sandbox actor uses it only for trusted requests; an untrusted runner forwards its
output as `PRINT` events instead.

An exception out of the raced block is formatted by
`BaseModelDeployment.format_error` and becomes the ERROR description verbatim: a
`RequestError` (unreadable payload, architecture mismatch) reports its own sentence;
anything else goes through nnsight's `clean_traceback`, which drops nnsight
internals, then `filter_traceback`, which drops this actor's wrapper frames, leaving
the user's code with the line text the linecache registration supplied. It also
returns a *fatal* flag — the two CUDA messages in `_UNRECOVERABLE_CUDA_ERRORS`
("device-side assert triggered", "an illegal memory access was encountered") poison
the process's CUDA context permanently, so `finally` calls
`BaseModelDeployment.restart`, i.e. `ray.kill(current_actor, no_restart=False)`, and
`max_restarts=-1` brings the replica back. `BaseModelDeployment.error_name` names
the failure for telemetry (`error_type`): the exception's class name here; the
sandbox actor reports the runner-side cause instead of its `RunnerError` wrapper.

### The finally block, and the upload

`finally` exits the `block_scope()` (`src/ndif/services/ray/deployments/modeling/nns.py`) entered before the try, restoring
`linecache.cache` and nnsight's process-global `SOURCES`/`BLOCKS` memos from the
snapshots taken on entry — they're keyed by `(filename, line)` and never
re-validated, so a later request reusing the same trace-site would otherwise run
*this* request's stale compiled block. It then drops `run`'s own references to the
block's future: a failed block's exception lives on in it, and through its traceback
so do the block's frames and every tensor they hold, which cleanup's collection
could not otherwise reach. Then `restart()` on a fatal error, else
`BaseModelDeployment.cleanup`: clear the kill switch and `kill_reason`, drop
`execution_ident`, `model.interleaver.cancel()`, `synchronize` on **every** card in
`gpu_mem_bytes_by_id`, then `gc.collect` + `empty_cache`. Every card, because a block
that fails while the model is on a later card leaves that card's kernels queued, and
freeing their memory under them is an illegal memory access that fails the next
request.

`BaseModelDeployment.prepare_result` zstd-compresses (level 3) when
`request.compress` is set and meters the size. The blob then takes one of two routes
back, decided against the inline cap — `RedisProvider.max_publish_bytes`, derived
from the Redis server's own pubsub output-buffer limits: under the cap, and with a
`session_id` to publish to, it rides on the `COMPLETED` response's `data` field
itself, stored as `torch.save` output under the Redis key `result:{id}` with a
short notice on pub/sub, and forwarded by `/subscribe` as a binary frame. Over it — and for a non-blocking request, which has no socket —
`BaseModelDeployment.upload_bytes` `put`s it under `{request.id}.pt` and returns a
presigned GET url to ride on `data` instead. The url is signed with
`NDIF_OBJECT_STORE_PUBLIC_URL` when set: a presigned url is an HMAC over the request
*including the host*, so it must be signed with the host the downloader will hit
(`ObjectStoreProvider.presigned_get` in `src/ndif/common/providers/objectstore.py`
signs with `public_client`). Default expiry: one hour. The cap itself is
[Status and Results](../concepts/status-and-results.md)'s topic.

## Batching

The actor does **no cross-request batching** and serves one request at a time:
`Replica.dispatch` awaits `run.remote(request)` before pulling the next, and
per-request actor state (`execution_ident`, `kill_switch`) is single-slot, so
overlapping `run()`s would collide. Batching happens *inside* one request — nnsight's
`Batcher` combines a trace's invokes into one forward pass — and
`NDIF_DEFAULT_PADDING_FACTOR` / `NDIF_DEFAULT_PADDING_BIAS` are the headroom the
controller reserves for it when sizing a placement.

## Metrics and event logs

`BaseModelDeployment.report` emits a structured `event()` log for non-completed
outcomes and always emits `ExecutionTimeMetric`.

| Metric | Measurement | Emitted at | Key fields |
|---|---|---|---|
| `ModelLoadTimeMetric` | `model_load_time` | `load_from_disk`, `from_cache` | `duration_ms`, `load_type`, `num_gpus` |
| `GPUMemMetric` | `gpu_mem` | after a successful run | per device `baseline_bytes`, `peak_bytes`, `extra_bytes` |
| `RequestResponseSizeMetric` | `response_size` | `prepare_result`, on both routes | `response_bytes`, `compressed` |
| `ExecutionTimeMetric` | `execution_time` | every outcome | `exec_ms`, `deserialize_ms`, `upload_ms`, tag `status` ∈ `completed`/`error`/`timeout`/`cancelled`/`preempted` |
| `RequestStatusTimeMetric` | `status_time` | indirectly, on every `respond` | `duration_ms` of the status just left |

`exec_started` is stamped before the env swap and `exec_ms` measured once the race
resolves, so **`exec_ms` spans the whole worker-thread body and overlaps
`deserialize_ms`** rather than excluding it — true on the sandbox path too. GPU
attribution is `gpu_baselines` before the run and `gpu_peaks` after (`src/ndif/services/ray/deployments/modeling/util.py`).

## Lifecycle

- **Readiness.** There is no health RPC; the controller and the queue wait on
  `actor.__ray_ready__.remote()` (`_ControllerActor.apply`, `Replica.wait`), which
  resolves only once `__init__` — including the weight load — has returned.
- **Failure.** `max_restarts=-1`: Ray restarts the actor on process death and the new
  process reloads the model. To the queue an `ActorDiedError` is an eviction —
  re-queue at the front, drop the replica, re-provision if work remains — while an
  `ActorUnavailableError` (a restart in progress) errors that request and parks the
  replica's worker until the actor is back.
- **Eviction / shutdown.** `Deployment.delete()` is `ray.kill(actor,
  no_restart=True)`. There is no graceful-shutdown hook — nothing calls
  `close()`/`__del__`, so a subclass holding OS resources loses them with the
  process.

## The template hooks

`ModelActor` (`src/ndif/services/ray/deployments/modeling/base.py`, decorated `@ray.remote(num_cpus=1, max_restarts=-1)`) is the
entire default actor: an empty `BaseModelDeployment` subclass. What a different actor
changes lives in six methods `run()` calls — `execute`, `execution_scope`,
`interrupt`, `format_error`, `error_name`, `cleanup` — plus the `commit` seam inside
`execute` and `restart`. `SandboxModelDeployment`
(`src/ndif/services/ray/sandbox/model.py`) overrides all six and `restart`.
`NDIF_MODEL_IMPORT_PATH` picks the class the controller builds each deployment from,
resolving `NDIF_MODEL_IMPORT_PATH` → `NDIF_DEFAULT_MODEL_ACTOR_CLASS` → the base
`ModelActor` (`ControllerDeploymentArgs.model_import_path` in `src/ndif/services/ray/deployments/controller/controller.py`). The compose
stack sets `NDIF_DEFAULT_MODEL_ACTOR_CLASS` to
`ndif.services.ray.sandbox.model.SandboxModelActor` (compose service `ray`,
`NDIF_DEFAULT_MODEL_ACTOR_CLASS`), which — absent `NDIF_MODEL_IMPORT_PATH` — wins,
even though the code default is `...modeling.base.ModelActor`: the compose stack
runs sandboxed, a bare controller runs in-process. A per-deployment
`DeploymentConfig.actor_class` overrides whichever the env resolves to.

## Related

- [Adding a Model Actor](adding-a-model-actor.md) — the recipe for writing your own
  actor around those hooks.
- [Sandbox Internals](sandbox-internals.md) — the runner process and the split
  interleaver.
- [Controller Internals](controller-internals.md) — who creates, caches, and kills
  these actors; [Queue Internals](queue-internals.md) — the worker that calls `run`.
- [Status and Results](../concepts/status-and-results.md) — statuses and result
  blobs from the client's side.
