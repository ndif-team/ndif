---
title: Adding a Model Actor
one_liner: Recipe for writing your own model actor — what to subclass, which seams of the run template are the real extension points, the contract to keep, and how the controller picks your class.
tags: [dev, internals, ray]
related: [docs/developing/model-actor.md, docs/developing/sandbox-internals.md, docs/developing/controller-internals.md, docs/operating/models-and-deployment.md, docs/concepts/sandbox-execution.md, docs/reference/env-vars.md, docs/developing/contributing.md]
sources: [src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/sandbox/driver.py, src/ndif/services/ray/tp/model.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/common/schema/controller.py, src/ndif/cli/lib/deploy.py, src/ndif/cli/lib/models.py, src/ndif/cli/lib/model_config.py]
---

# Adding a Model Actor

## What this covers

A "model actor" is the class the controller deploys once per replica to own a loaded
model and execute requests against it. It is a plain **detached Ray actor** — not a
Ray Serve deployment — selected by a dotted import path. Adding one is: subclass
`BaseModelDeployment`, override the seams that differ, wrap it in `@ray.remote`, and
point `NDIF_MODEL_IMPORT_PATH` (or a per-deployment `actor_class`) at it.

What the base class already does — loading, GPU placement, HOT/WARM, statuses,
upload, metrics — is [Model actor](model-actor.md); this page assumes it.

Write a new actor when execution has to happen somewhere else (another process, a
different engine, a stub for tests) or when errors and cleanup need different
handling. Not to change how the model *loads* — that is the model key,
`BaseModelDeploymentArgs`, and nnsight's model wrapper class.

## What you subclass

```python
# src/ndif/services/ray/mystack/model.py
import ray
from ..deployments.modeling.base import BaseModelDeployment


class MyModelDeployment(BaseModelDeployment):
    """Plain-Python behaviour; unit-testable without Ray."""


@ray.remote(num_cpus=1, max_restarts=-1)
class MyModelActor(MyModelDeployment):
    """The deployable actor."""
```

Keep the split the tree uses: the *deployment* class holds the logic, the
`@ray.remote` subclass is a one-line wrapper — `ModelActor` in
`src/ndif/services/ray/deployments/modeling/base.py`, and likewise
`SandboxModelActor` in the sandbox package. Keep `max_restarts=-1`:
`BaseModelDeployment.restart` kills the actor with `no_restart=False` after a
CUDA-context-corrupting failure and relies on Ray to bring it back.

Your `__init__` must accept what the controller passes — the fields of
`BaseModelDeploymentArgs`: `model_key`, `execution_timeout`, `gpu_mem_bytes_by_id`,
`dtype` — via `**kwargs`, and call `super().__init__()`. The
controller creates whichever class it resolves the same way
(`src/ndif/services/ray/deployments/controller/cluster/deployment.py`
`Deployment.create`): named `{replica_id}:ModelActor:{model_key}` in namespace
`NDIF`, `lifetime="detached"`, pinned to a node with `resources={"node:<name>": 0.01}`
and no `num_gpus` (GPU targeting happens inside the actor via `max_memory`), and a
`runtime_env` carrying provider config.

> **Gotcha:** a constructor parameter of your own has no path from the controller.
> `SandboxModelDeployment.__init__` takes `pool_size` and nothing can set it, which
> is why its default comes from `NDIF_SANDBOX_POOL_SIZE` — in any deployment, that
> env value is what applies. Read your own options from the environment, or add a
> field to `BaseModelDeploymentArgs` and teach `DeploymentConfig` and the controller
> to populate it.

## The seams

`BaseModelDeployment.run` is a template method. It publishes `RUNNING`, snapshots
GPU counters and the source-cache globals, applies `request.env`, races `execute()`
on a worker thread against `execution_timeout` and the `kill_switch`, emits metrics
and events, sends the result back (inline or through the object store), publishes
`COMPLETED`/`ERROR`, and restores state in `finally`. Everything a different actor
needs to change is in the methods it calls:

| Seam | Base behaviour | Override when |
|---|---|---|
| `execute(request) -> (bytes, float \| None)` | build the block against `self.model`, run it, `torch.save` the saved values | the block should run anywhere other than this thread/process |
| `execution_scope(request)` | context around the raced wait; redirects stdout *and* stderr into a `LogStream` each, so prints and warnings become `LOG` responses | your executor reports output another way |
| `interrupt()` | `kill_thread(self.execution_ident)` — inject `SystemExit` into the execution thread | there is something else to stop: a subprocess, a socket, an engine request |
| `format_error(exc) -> (str, bool)` | strip nnsight and actor frames; flag unrecoverable CUDA errors as fatal | your errors arrive pre-formatted, or a different failure is fatal |
| `error_name(exc) -> str` | the exception's class name, recorded as telemetry `error_type` | your failures arrive wrapped and you can name the real cause |
| `cleanup()` | clear the kill switch, `kill_reason` and `execution_ident`, cancel the interleaver, `synchronize`/`gc`/`empty_cache` | you hold per-request resources; call `super().cleanup()` |
| `restart()` | `ray.kill(..., no_restart=False)` for a fresh CUDA context | you hold resources that would outlive the actor; release them, then `super().restart()` |
| `commit()` | nothing | something must happen exactly between "the block built" and "the block runs" — `TPModelDeployment.commit` releases the other ranks there |

`SandboxHost` (`src/ndif/services/ray/sandbox/model.py`) adds two more, called by its
`run_in_runner` around sending the block to a runner: `before_payload(request,
sandbox, seed)` and `after_payload()`. They are no-ops for one host; the
tensor-parallel actor uses them to tell its shards where the runner is, then release
them into the forward.

> **If you call `cancel()` yourself**, mind the `reason`. Unset means "genuinely
> cancelled": the user is told and the request stops. Pass `KILL_REASON_PREEMPTED`
> only when the request is blameless and still runnable — `run` then raises
> `CachedActorError` and the queue re-queues it at the *front* of the line, so
> mislabelling a deliberate cancellation re-runs it indefinitely.

Everything else — `load_from_disk`, `to_cache`/`from_cache`, `cancel`, `report`,
`prepare_result`, `upload_bytes`, and `run` itself — should be inherited. Overriding
`run` means re-implementing the contract below, which the queue, the dashboard and
Grafana all depend on.

## The contract you must uphold

Replace `execute` only and you get all of this for free. Go further and keep it:

1. **Exactly one terminal response per request** — `COMPLETED` or `ERROR` after
   `RUNNING` (or a `CachedActorError` re-queue). A request with no terminal response
   hangs the client's websocket.
2. **`execute` returns `(blob, deserialize_ms)`**: the `torch.save` bytes, and the
   deserialize time or `None` (the metric field is then dropped). `run` decides the
   route — inline under `RedisProvider.max_publish_bytes()` on a request with a live
   socket, otherwise `upload_bytes` to `{request.id}.pt` and a presigned URL — and
   `prepare_result` compresses iff `request.compress`.
3. **Saved values are `nnsight.save()`-marked locals**, matched by identity in the
   block's frame. Reuse `execute_traced_block` and the shared `cpu_pickle_module`
   (`src/ndif/services/ray/deployments/modeling/nns.py`,
   `src/ndif/services/ray/deployments/modeling/util.py`) so the blob format and the
   autocast region are identical.
4. **Be interruptible.** `run` calls `interrupt()` on timeout or cancel and returns
   *without waiting for the thread*. If your executor can block outside Python (a
   socket `recv`, a subprocess), `interrupt()` must unblock it.
5. **Set `self.execution_ident`** on the worker thread before anything can block —
   `cancel()` is gated on it, so without it a cancel or preempt is silently dropped.
6. **Raise `CachedActorError` when WARM** — the base does it before its `try`, and
   the queue reads it as an eviction.
7. **Don't let request state outlive the request.** `cleanup()` runs after every
   outcome; the base's `block_scope` restores the global linecache and nnsight memos
   around your `execute`, which matters only if you deserialize in-process.
   The reverse also holds: the constructor `gc.freeze()`s every object alive
   after `load_from_disk`, so per-request state must not exist yet at that
   point, or it is never collected.

## Copy the sandbox actor

`SandboxModelDeployment` is the reference implementation: the model loads on the host
through the base, an untrusted request's block runs in a separate runner process, and
it overrides seams and nothing else. Read it in
`src/ndif/services/ray/sandbox/model.py` (240 lines) and the walk-through in
[Sandbox internals](sandbox-internals.md#the-actors-seams). What to copy:

- **Put shared hosting logic in a mixin.** `SandboxHost` holds everything about
  hosting a runner; `SandboxModelDeployment` mixes it over `BaseModelDeployment`, and
  `SandboxedTPModelDeployment` (`src/ndif/services/ray/tp/model.py`) mixes the same one over `TPModelDeployment`.
- **Delegate rather than duplicate.** The trusted branch of `execute` is
  `super().execute(request)` — one in-process path, not two.
- **Extend, don't replace.** `interrupt`, `cleanup` and `restart` do their own work
  and then call `super()`.
- **Return an already-serialized blob.** The runner does the `torch.save`, so the
  actor hands the bytes back with no unpickle/repickle round trip.
- **Keep the engine out of the actor.** `SandboxDriver`
  (`src/ndif/services/ray/sandbox/driver.py`) drives the model for a runner and knows
  nothing about Ray or requests, which is what lets a tensor-parallel shard reuse it.

## Selecting your actor

`Deployment._resolve_actor_class` accepts a dotted import path — imported *inside the
Ray worker*, so it must be importable there — or an already-`@ray.remote` class. The
value is chosen at placement (`src/ndif/services/ray/deployments/controller/cluster/cluster.py`
`Cluster.deploy`), first match wins:

| Source | Scope |
|---|---|
| `DeploymentConfig.actor_class` (`src/ndif/common/schema/controller.py`) | one deploy call |
| `NDIF_TP_MODEL_ACTOR_CLASS` (`Evaluator.actor_class`) | a replica given more than one GPU that shards evenly into exactly that many |
| `NDIF_MODEL_IMPORT_PATH` | the whole cluster |
| `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | fallback for the above |
| `ndif.services.ray.deployments.modeling.base.ModelActor` | the built-in default |

The last three are read by
`src/ndif/services/ray/deployments/controller/controller.py`
`ControllerDeploymentArgs`. The controller substitutes its default for `None` before
create, so a `None` reaching `_resolve_actor_class` is a bug.

Per deployment:

```bash
ndif deploy openai-community/gpt2 --actor-class ndif.services.ray.mystack.model.MyModelActor
```

or in a `models.yaml` for `ndif deploy -f` (format in
`src/ndif/cli/lib/model_config.py`):

```yaml
models:
  - checkpoint: openai-community/gpt2
    replicas: 1
    actor_class: ndif.services.ray.mystack.model.MyModelActor
```

Cluster-wide, set `NDIF_MODEL_IMPORT_PATH` on the ray service. The compose stack
selects the sandbox actor through `NDIF_DEFAULT_MODEL_ACTOR_CLASS` instead; with
`NDIF_MODEL_IMPORT_PATH` unset the two are equivalent.

Changing the default affects only **new** deployments: the class is captured on each
`Deployment` at placement, so evict and re-deploy a model to move it.

## Checking it works

`ndif deploy` blocks on each new replica's `__ray_ready__`
(`src/ndif/cli/lib/models.py` `wait_for_replica_ready`), which resolves only after
`__init__` — including the weight load — returns. `ndif status --verbose
--json-output` dumps controller state, where each replica carries its resolved
`actor_class` (`Deployment.get_state`). Then run a real `remote=True` trace and check
for a terminal status and a result; see [Testing](testing.md).

## Related

[Model actor](model-actor.md) for the base class; [Sandbox internals](sandbox-internals.md)
for the reference implementation; [Controller internals](controller-internals.md) for
how a `Deployment` becomes an actor;
[Models and deployment](../operating/models-and-deployment.md) for deploying, pinning
and evicting; [Environment variables](../reference/env-vars.md) for the actor-class
variables.
