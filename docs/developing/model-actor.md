---
title: Model Actor
one_liner: The Ray actor that owns a model's GPU-resident weights and runs one request against them — loading, the run() template, results, timeouts, and metrics.
tags: [internals, dev, ray, telemetry]
related: [docs/developing/adding-a-model-actor.md, docs/developing/sandbox-internals.md, docs/developing/controller-internals.md, docs/developing/queue-internals.md, docs/concepts/request-lifecycle.md, docs/concepts/status-and-results.md, docs/concepts/sandbox-execution.md, docs/concepts/deployments-and-eviction.md, docs/developing/nnsight-integration.md, docs/reference/env-vars.md]
sources: [src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/deployments/modeling/util.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/common/schema/request.py, src/ndif/common/providers/objectstore.py, src/ndif/common/metrics.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/auth.py, src/ndif/services/ray/sandbox/model.py]
---

# Model Actor

## What this covers

`BaseModelDeployment` (`src/ndif/services/ray/deployments/modeling/base.py:103`) is
the class that holds a model: one instance per replica, each a detached Ray actor in
its own worker process, created by the controller and called by the queue. This page
covers its constructor args, weight loading and GPU placement, HOT↔WARM caching, the
per-request `run()` path, statuses and results, timeouts, error handling, metrics,
and lifecycle. Three facts shape the design:

1. **The actor outlives requests.** Weights stay resident for hours, so a request's
   leftovers — linecache entries, nnsight's compiled-block memo, CUDA activations —
   would leak into the next user's run if they weren't scrubbed.
2. **The code it runs is arbitrary user Python.** It must be timeboxed, cancellable
   mid-flight, and when it raises, the user's own traceback has to come back to them.
3. **The base actor is the *trusted* execution path** — its `execute` runs the block
   in this process, next to the weights. Untrusted requests are diverted to a
   separate runner process; see [below](#trusted-and-untrusted-requests).

`run()` is a **template method** built around those facts: a subclass overrides a few
hooks and inherits the rest — recipe in [adding-a-model-actor.md](./adding-a-model-actor.md).

## Construction

A replica is a plain **detached Ray actor**, not a Ray Serve deployment.
`Deployment.create` (`controller/cluster/deployment.py:192`) does
`actor_class.options(name=..., resources={f"node:{node}": 0.01}, namespace="NDIF",
lifetime="detached", runtime_env=...).remote(**deployment_args.model_dump())`, where
`deployment_args` is a `BaseModelDeploymentArgs` (`base.py:81`).

| Field | Default | Where it comes from |
|---|---|---|
| `model_key` | required | The nnsight model key (`"{import.path}:{json}"`) this deployment serves |
| `execution_timeout` | `None` | `DeploymentConfig.execution_timeout_seconds`, else `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` (`3600`) — `controller.py:271` |
| `gpu_mem_bytes_by_id` | `{}` | Injected just before create from the placement the cluster chose (`deployment.py:172`) |
| `dtype` | `"bfloat16"` | `DeploymentConfig.dtype`, else `NDIF_DEFAULT_DTYPE` (`controller.py:279`) |
| `trust_remote_code` | `False` | `DeploymentConfig.trusted`, from the `trusted` flag of the request that triggered the deployment (`controller.py:280`, `cluster.py:169`) |

Only these five fields cross the boundary — a constructor parameter that isn't a
field here can never be set by the controller. `__init__` (`base.py:106`) binds the
first four by name; anything else lands in `self.kwargs`, forwarded verbatim into
`from_model_key` at load time, which is how `trust_remote_code` reaches transformers.

`__init__` connects Loki and Influx first (`base.py:117`) — each Ray actor is its own
process, so sinks installed on the driver don't exist here. Their config arrives via
the actor's `runtime_env`, filled from the controller's environment
(`_provider_runtime_env`, `deployment.py:174`) along with `NDIF_SERVICE=model`,
`PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`, and
`RAY_EXPERIMENTAL_NOSET_CUDA_VISIBLE_DEVICES=1` — that last one matters, because the
actor sees *every* GPU on the node and targets them itself through `max_memory`. The
actor registers as `{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace
(`Deployment.name`, `deployment.py:106`), which is what the queue looks up
(`get_model_actor_handle`, `common/providers/ray.py:217`).

## Loading the model

`load_from_disk` (`base.py:144`) runs in the constructor: the actor isn't `__ray_ready__` until the weights are on the GPUs.

```python
set_default_gpu(self.gpu_mem_bytes_by_id)      # util.py:91  — before any CUDA call
set_process_limits(self.gpu_mem_bytes_by_id)   # util.py:119 — per-process fraction
max_memory = build_max_memory(self.gpu_mem_bytes_by_id)   # util.py:151
model = HuggingFaceModel.from_model_key(
    self.model_key, device_map="balanced", max_memory=max_memory,
    dispatch=True, torch_dtype=self.dtype, **self.kwargs)
```

- `set_default_gpu` pins the default CUDA device to the first assigned GPU: the
  ~400 MiB CUDA context is created on the *current* device, so without it it lands on
  `cuda:0`, a card this replica may not own. `set_process_limits` caps the allocator
  per device at the granted budget; `build_max_memory` clamps each budget to physical
  memory so accelerate can't plan an impossible fit.
- `from_model_key` is nnsight's `Remotable` classmethod: it splits the class import
  path off the key and delegates to that wrapper's `_remoteable_from_model_key`. For
  a HuggingFace model the rest of the key is `{"repo_id": ..., "revision": ...}`, so
  **the revision is part of the model key**, not a separate arg. `dispatch=True` skips
  nnsight's meta-device phase; `device_map`/`max_memory`/`torch_dtype` ride into the
  transformers model kwargs.
- After a `torch.cuda.synchronize()` (accelerate's copies are async),
  `verify_device_placement` (`util.py:166`) walks every parameter and buffer and
  raises if one is still on `meta`/`cpu`, sits on an unassigned card, or if an
  assigned card holds nothing. Grads are disabled and `ModelLoadTimeMetric` is
  emitted with `load_type="initial"` (`base.py:170`).

> **Gotcha:** `dtype` and `trust_remote_code` must match what the controller's
> evaluator sized this model with, or the GPU accounting that placed the replica is
> wrong. Hence the controller pins `config.dtype` before evaluating (`controller.py:127`).

PEFT adapters are **not** a load-time concern: the base model is named by the model
key, and an adapter travels per request in `request.env`, applied by
`_remoteable_set_env` (`base.py:294`) just before execution. nnsight's
`TransformersModel` rewraps the module only when the requested adapter differs from
the current one, so a repeat request pays nothing.

## HOT ↔ WARM

The controller moves a replica between levels through two actor methods (`deployment.py:153`, `:161`):

| Call | Method | What happens |
|---|---|---|
| HOT→WARM | `to_cache()` (`base.py:186`) | `cancel()` any in-flight run, `_module.to("cpu")`, `reset_process_limits`, `synchronize` + `gc.collect` + `empty_cache`, `self.cached = True` |
| WARM→HOT | `from_cache(gpus)` (`base.py:206`) | Re-apply limits for the (possibly different) GPUs, compute a balanced `device_map`, `remove_accelerate_hooks` then `dispatch_model`, verify, `self.cached = False`, emit `ModelLoadTimeMetric(load_type="from_cache")` |

`remove_accelerate_hooks` (`util.py:106`) is not optional — re-dispatching an
already-dispatched module stacks a second set of hooks on the first. While `cached`
is true, `run()` raises `CachedActorError` *before* its try block (`base.py:258`) so
it reaches the queue as an eviction rather than a user-facing ERROR; `Replica.dispatch`
treats it like a dead actor — drop the replica, re-queue at the front
(`replica.py:52`, `:231`).

## Trusted and untrusted requests

`BackendRequestModel.trusted` (`common/schema/request.py:57`, default `False`) is
stamped at ingress from the api key's `trusted` user_tag
(`src/ndif/services/api/auth.py:178`); **with auth off — no `NDIF_POSTGRES_URL` —
it defaults to trusted but honors a client-supplied `trusted: false`** (`auth.py:184`).
The flag decides where the block runs:

- **Trusted** → `BaseModelDeployment.execute` (`base.py:379`), in the actor process,
  in the same address space as the weights. That is the path documented below.
- **Untrusted** → `SandboxModelDeployment.execute` (`sandbox/model.py:242`) ships the
  payload to a runner **process** and drives the model over a socket. Isolation is
  process-based and still in progress: the runner is an ordinary OS process with no
  hardening. Everything in `run()` around `execute` is the same code either way.

## One request, inside the actor

The queue's replica worker calls `handle.run.remote(request)` and awaits it
(`Replica.dispatch`, `replica.py:203`). The request carries `payload` (the serialized
tracer blob), `compress`, `env`, `trusted`, and the identity fields (`id`, `api_key`,
`email`, `session_id`) used for responses and telemetry (`request.py:44`).

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
    R->>T: to_thread execute, raced vs timeout and kill_switch
    T->>T: RequestModel.deserialize payload
    T->>M: tracer.execute — interleaved forward pass
    T-->>C: respond LOG per printed line
    T-->>R: torch.save blob, deserialize_ms
    R->>S: put request-id.pt, then presign a GET url
    R->>R: finally restore snapshots, cleanup
    R->>C: respond COMPLETED with url
```

### The race

Execution runs on a worker thread so the event loop can race it (`base.py:298`):

```python
job = asyncio.create_task(asyncio.to_thread(self.execute, request))
kill = asyncio.create_task(self.kill_switch.wait())
with self.execution_scope(request):
    done, pending = await asyncio.wait({job, kill},
        timeout=self.execution_timeout, return_when=asyncio.FIRST_COMPLETED)
```

| Winner | Response | `report()` status |
|---|---|---|
| `job` | continue to upload → `COMPLETED` | `completed` |
| `kill` (`cancel()` set the switch) | `ERROR` "Your job was cancelled or preempted by the server." | `cancelled` |
| neither (timeout) | `ERROR` "Your job exceeded the execution timeout of `{n}`s." | `timeout` |

The timeout covers only the raced wait — deserialize plus execute; the env swap
before it and the upload after it sit outside. On cancel or timeout `interrupt()`
(`base.py:440`) calls `kill_thread` (`util.py:69`), injecting `SystemExit` via
`PyThreadState_SetAsyncExc` — which fires only at a Python bytecode boundary, so it
**cannot** interrupt a CUDA kernel or native op already in flight.

### The worker-thread body

`execute` (`base.py:379`) is the hook a subclass replaces to run the block elsewhere:

```python
persistent_objects = self.model._remoteable_persistent_objects()
tracer = RequestModel.deserialize(request.payload, persistent_objects,
                                  compress=request.compress)
# autocast is thread-local, so it lives here, not in run(); half precision only
with torch.autocast(device_type="cuda", dtype=self.dtype, enabled=autocast_enabled):
    inc()
    try:
        tracer.execute(tracer.info.code)
        saves = _saves()
        saved = {n: v for n, v in tracer.info.frame.f_locals.items() if id(v) in saves}
    finally:
        dec()
buffer = io.BytesIO()
torch.save(saved, buffer, pickle_module=cpu_pickle_module())
return buffer.getvalue(), deserialize_ms
```

- The persistent-object map is recomputed *after* the env swap, so a PEFT rebind is
  reflected in the `Module:<path>` ids the payload resolves against.
- nnsight's `RequestModel.deserialize` recompiles the block from its shipped
  **source**, not bytecode, and registers it in `linecache` so a traceback can show
  the user's line even though their file doesn't exist here.
- `inc()`/`dec()` bracket a trace scope the way the client's `Tracer.__exit__` does,
  so `nnsight.save()` records into this thread's save set. Saves are matched **by
  identity** against frame locals — an unbound `.save()` has no name and never
  appears in the result.
- `cpu_pickle_module()` (`util.py:269`) is a `pickle` stand-in whose `Pickler`
  relocates CUDA tensors to CPU first, yielding materially smaller blobs.

### Statuses, logs, and errors

Everything the client sees comes from `BackendRequestModel.respond`
(`common/schema/request.py:134`): a blocking request (has `session_id`) is published
to that Redis channel and forwarded by the API's `/subscribe` websocket; a
non-blocking one has its latest response written to `responses/{id}.json` for
`GET /response/{id}`. The actor emits `RUNNING` on entry (`base.py:261`), zero or
more `LOG`s, and exactly one `COMPLETED`/`ERROR`. `LOG` comes from `execution_scope`
(`base.py:428`), which redirects `sys.stdout` to a `LogStream` (`util.py:16`)
emitting one `LOG` per complete line; the redirect is installed on the event-loop
thread, since `sys.stdout` is process-global and the restore must not run on the
thread `interrupt()` kills.

An exception out of the raced block is formatted by `format_error` (`base.py:444`)
and becomes the ERROR description verbatim: nnsight's `clean_traceback` drops nnsight
internals, then `filter_traceback` drops this actor's wrapper frames, leaving the
user's code with the line text the linecache registration supplied. It also returns a
*fatal* flag — the two CUDA messages at `base.py:70` ("device-side assert triggered",
"an illegal memory access was encountered") poison the process's CUDA context
permanently, so `finally` calls `restart()` (`base.py:511`), i.e. `ray.kill(
current_actor, no_restart=False)`, and `max_restarts=-1` brings the replica back.

### The finally block, and the upload

`finally` (`base.py:355`) restores `linecache.cache` and nnsight's process-global
`SOURCES`/`BLOCKS` memos from the snapshots taken at entry — they're keyed by
`(filename, line)` and never re-validated, so a later request reusing the same
trace-site would otherwise run *this* request's stale compiled block. Then
`restart()` on a fatal error, else `cleanup()` (`base.py:521`): clear the kill switch,
drop `execution_ident`, `model.interleaver.cancel()`, and `synchronize` +
`gc.collect` + `empty_cache`.

`upload_bytes` (`base.py:536`) zstd-compresses (level 3) when `request.compress` is
set, `put`s the blob under `{request.id}.pt`, and returns a presigned GET url that
rides back on the `COMPLETED` response's `data` field. The url is signed with
`NDIF_OBJECT_STORE_PUBLIC_URL` when set: a presigned url is an HMAC over the request
*including the host*, so it must be signed with the host the downloader will hit
(`objectstore.py:159`). Default expiry: one hour.

## Batching

The actor does **no cross-request batching** and serves one request at a time:
`Replica.dispatch` awaits `run.remote(request)` before pulling the next, and
per-request actor state (`execution_ident`, `kill_switch`) is single-slot, so
overlapping `run()`s would collide. Batching happens *inside* one request — nnsight's
`Batcher` combines a trace's invokes into one forward pass — and
`NDIF_DEFAULT_PADDING_FACTOR` / `NDIF_DEFAULT_PADDING_BIAS` are the headroom the
controller reserves for it when sizing a placement.

## Metrics and event logs

`report()` (`base.py:456`) emits a structured `event()` log for non-completed
outcomes and always emits `ExecutionTimeMetric`.

| Metric | Measurement | Emitted at | Key fields |
|---|---|---|---|
| `ModelLoadTimeMetric` | `model_load_time` | `load_from_disk`, `from_cache` | `duration_ms`, `load_type`, `num_gpus` |
| `GPUMemMetric` | `gpu_mem` | after a successful run (`base.py:331`) | per device `baseline_bytes`, `peak_bytes`, `extra_bytes` |
| `RequestResponseSizeMetric` | `response_size` | `upload_bytes` | `response_bytes`, `compressed` |
| `ExecutionTimeMetric` | `execution_time` | every outcome | `exec_ms`, `deserialize_ms`, `upload_ms`, tag `status` ∈ `completed`/`error`/`timeout`/`cancelled` |
| `RequestStatusTimeMetric` | `status_time` | indirectly, on every `respond` | `duration_ms` of the status just left |

`exec_started` is stamped before the env swap and `exec_ms` measured once the race
resolves (`base.py:283`, `:311`), so **`exec_ms` spans the whole worker-thread body
and overlaps `deserialize_ms`** rather than excluding it — true on the sandbox path
too. GPU attribution is `gpu_baselines` before the run and `gpu_peaks` after
(`util.py:210`, `:229`).

## Lifecycle

- **Readiness.** There is no health RPC; the controller and the CLI wait on
  `actor.__ray_ready__.remote()` (`controller.py:287`, `replica.py:141`), which
  resolves only once `__init__` — including the weight load — has returned.
- **Failure.** `max_restarts=-1`: Ray restarts the actor on process death and the new
  process reloads the model. To the queue an `ActorDiedError` is an eviction —
  re-queue at the front, drop the replica, re-provision if work remains.
- **Eviction / shutdown.** `Deployment.delete()` is `ray.kill(actor,
  no_restart=True)` (`deployment.py:141`). There is no graceful-shutdown hook —
  nothing calls `close()`/`__del__`, so a subclass holding OS resources loses them
  with the process.

## The template hooks

`ModelActor` (`base.py:565`, decorated `@ray.remote(num_cpus=1, max_restarts=-1)`) is
the entire default actor: an empty `BaseModelDeployment` subclass. What a different
actor changes lives in five methods `run()` calls — `execute`, `execution_scope`,
`interrupt`, `format_error`, `cleanup` — and `SandboxModelDeployment`
(`sandbox/model.py:170`) overrides exactly those five.
`NDIF_MODEL_IMPORT_PATH` picks the class the controller builds each deployment from,
resolving `NDIF_MODEL_IMPORT_PATH` → `NDIF_DEFAULT_MODEL_ACTOR_CLASS` → the base
`ModelActor` (`ControllerDeploymentArgs`, `controller.py:554-560`). The compose stack
sets `NDIF_DEFAULT_MODEL_ACTOR_CLASS` to
`ndif.services.ray.sandbox.model.SandboxModelActor` (`docker/docker-compose.yml:228`),
which — absent `NDIF_MODEL_IMPORT_PATH` — wins, even though the code default is
`...modeling.base.ModelActor`. That split is intentional: the dev/compose stack runs
sandboxed, a bare controller runs in-process. A per-deployment `DeploymentConfig.actor_class`
overrides whichever the env resolves to.

## Related

- [adding-a-model-actor.md](./adding-a-model-actor.md) — the recipe for writing your own actor around those five hooks.
- [sandbox-internals.md](./sandbox-internals.md) — the runner process and the split interleaver.
- [controller-internals.md](./controller-internals.md) — who creates, caches, and kills these actors; [queue-internals.md](./queue-internals.md) — the worker that calls `run`.
- [../concepts/status-and-results.md](../concepts/status-and-results.md) — statuses and result blobs from the client's side.
