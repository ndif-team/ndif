---
title: Adding a Model Actor
one_liner: Recipe for writing your own model actor — what to subclass, which five hooks are the real extension points, and how the controller picks your class.
tags: [dev, internals, ray]
related: [docs/developing/model-actor.md, docs/developing/sandbox-internals.md, docs/developing/controller-internals.md, docs/operating/models-and-deployment.md, docs/concepts/sandbox-execution.md, docs/reference/env-vars.md, docs/developing/contributing.md]
sources: [src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/sandbox/driver.py, src/ndif/services/ray/tp/model.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/common/schema/controller.py, src/ndif/cli/lib/deploy.py, src/ndif/cli/lib/models.py, src/ndif/cli/lib/model_config.py]
---

# Adding a Model Actor

## What this covers

A "model actor" is the class the controller deploys once per replica to own a loaded
model and execute requests against it. It is a plain **detached Ray actor** — not a
Ray Serve deployment — selected by a dotted import path, so adding one is: subclass
`BaseModelDeployment`, override the hooks that differ, wrap it in `@ray.remote`, and
point `NDIF_MODEL_IMPORT_PATH` (or a per-deployment `actor_class`) at it.

This page is the recipe. For what the base class already does — loading, GPU
placement, HOT/WARM, statuses, upload, metrics — read
[model-actor.md](./model-actor.md) first; this page assumes it.

Write a new actor when execution has to happen somewhere else (another process, a
different engine, a stub for tests) or when errors and cleanup need different
handling. Do **not** write one to change how the model *loads* — that's the model key
plus `BaseModelDeploymentArgs`, and nnsight's model wrapper class.

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

Keep the split the tree already uses: the *deployment* class holds the logic, the
`@ray.remote` subclass is a one-line wrapper. `ModelActor`
(`services/ray/deployments/modeling/base.py:701`) and `SandboxModelActor`
(`services/ray/sandbox/model.py:223`) are both empty subclasses over their deployment
class, and both carry `num_cpus=1, max_restarts=-1` — keep `max_restarts=-1` unless
you want a replica that never comes back, since `BaseModelDeployment.restart()` kills
the actor with `no_restart=False` after a CUDA-context-corrupting failure and relies
on Ray to bring it back.

Your `__init__` must accept the same arguments the controller passes — `model_key`,
`execution_timeout`, `gpu_mem_bytes_by_id`, `dtype`, plus `**kwargs` — and call
`super().__init__()`. Those are the fields of `BaseModelDeploymentArgs`
(`base.py:83`), and the controller sends nothing else; the fifth field,
`trust_remote_code`, arrives through `**kwargs` and is forwarded to the loader:

```python
actor_class.options(                                   # deployment.py:210
    name=self.name,                                    # "{replica_id}:ModelActor:{model_key}"
    resources={f"node:{node_name}": 0.01},             # pin to the chosen node
    namespace="NDIF",
    lifetime="detached",                               # survives the controller
    runtime_env={"env_vars": env_vars},                # provider config + CUDA flags
).remote(**deployment_args.model_dump())
```

Your class doesn't set any of that — the controller does, for whichever class it
resolves. Note `resources={f"node:{node_name}": 0.01}` and no `num_gpus`: placement is
by node, and GPU targeting happens inside the actor via `max_memory`.

> **Gotcha:** an extra constructor parameter of your own has no path from the
> controller. `SandboxModelDeployment.__init__` takes `pool_size`
> (`sandbox/model.py:194`) and nothing can set it — which is why its default falls
> back to `NDIF_SANDBOX_POOL_SIZE` (`DEFAULT_POOL_SIZE`, `:47`) rather than a
> literal, and it is that env value in production.
> Read your own options from the environment (the actor's `runtime_env` carries the
> provider config already) or add a field to `BaseModelDeploymentArgs`, which also
> means teaching `DeploymentConfig` and the controller to populate it.

## The five extension points

`run()` (`base.py:300`) is a template method. It publishes `RUNNING`, snapshots GPU
counters and the linecache/`SOURCES`/`BLOCKS` globals, applies `request.env`, races
`execute()` on a worker thread against `execution_timeout` and the `kill_switch`,
emits metrics and event logs, sends the result back (inline or through the object
store), publishes `COMPLETED`/`ERROR`, and restores state in `finally`. Everything a
different actor needs to change is in five methods it calls:

| Hook | Base behaviour | Override when |
|---|---|---|
| `execute(request) -> (bytes, float\|None)` (`base.py:473`) | Deserialize the payload against `self.model`'s persistent objects, run the block under autocast, collect `nnsight.save()` values, `torch.save` them | The block should run anywhere other than this thread/process |
| `execution_scope(request)` (`base.py:516`) | Context manager around the raced wait; redirects `sys.stdout` *and* `sys.stderr` into a `LogStream` each, so prints and `warnings.warn` both become `LOG` responses | Your executor reports output another way (or not at all) |
| `interrupt()` (`base.py:541`) | `kill_thread(self.execution_ident)` | There is something else to stop — a subprocess, a socket, an engine request |
| `format_error(exc) -> (str, bool)` (`base.py:545`) | Clean nnsight + actor frames out of the traceback; flag unrecoverable CUDA errors as fatal | Your errors arrive pre-formatted, or a different failure class is fatal |
| `cleanup()` (`base.py:646`) | Clear the kill switch, `kill_reason` and `execution_ident`, cancel the interleaver, `synchronize`/`gc`/`empty_cache` | You hold per-request resources; call `super().cleanup()` |

A sixth seam sits one level down. `commit()` (`base.py:504`) is called by the base's
`execute` after the block is built and immediately before it runs — the point at
which a request has stopped being able to fail on a bad payload. It does nothing
here; override it when your actor has something to release at exactly that moment.
`TPModelDeployment.commit` (`tp/model.py:290`) is the one that does: it releases the
other ranks into the forward and arms the abort checkpoint.

> **If you call `cancel()` yourself**, mind the `reason`. The default (unset)
> means "genuinely cancelled": the user is told, and the request stops there.
> Pass `KILL_REASON_PREEMPTED` only when the request is blameless and still
> runnable — it makes `run` raise `CachedActorError` so the queue re-queues it,
> and a re-queue goes to the *front* of the line, so mislabelling a deliberate
> cancellation re-runs it forever.

Everything else — `load_from_disk`, `to_cache`/`from_cache`, `cancel`, `restart`,
`report`, `prepare_result`, `upload_bytes`, and `run` itself — should be inherited
unchanged. Overriding
`run` means re-implementing the contract below, and every consumer (the queue, the
dashboard, Grafana) depends on it.

## The contract you must uphold

If you replace `execute` only, you get all of this for free. If you go further, keep it:

1. **Exactly one terminal response per request.** `run()` publishes `RUNNING` on
   entry and precisely one `COMPLETED` or `ERROR`. A request with no terminal
   response hangs the client's websocket until it times out.
2. **`COMPLETED` carries either the bytes or a presigned url.** `execute` returns
   the `torch.save` bytes and `run()` decides the route (`base.py:418`): a result at
   or under `ObjectStoreProvider.max_socket_result_bytes` on a request with a live
   socket rides on the response itself, with `pickled=True` so `/subscribe` forwards
   a binary frame; anything larger, and every non-blocking request, goes through
   `upload_bytes` (`base.py:688`), staged at `{request.id}.pt` and signed as a GET
   url. `prepare_result` (`base.py:662`) compresses iff `request.compress` and meters
   the size, on both routes — the client decompresses on that same flag.
3. **Saved values are `nnsight.save()`-marked locals**, matched by identity in the
   block's frame. Whatever runs the block must apply the same rule; the sandbox's
   runner reuses `cpu_pickle_module` from
   `services/ray/deployments/modeling/util.py:287` so the blob format is identical.
4. **Return `(blob, deserialize_ms)`** from `execute`. `deserialize_ms` may be `None`
   if you can't measure it — the metric field is simply dropped.
5. **Be interruptible.** `run()` calls `interrupt()` on timeout or cancel and then
   returns *without waiting for the thread*. If your executor can block outside
   Python (a socket `recv`, a subprocess), `interrupt()` must unblock it, or the
   thread leaks and the next request collides with it.
6. **Raise `CachedActorError` when WARM**, which the base already does before its try
   block (`base.py:315`) — the queue reads that as an eviction and re-queues, instead
   of showing the user an error.
7. **Don't let request state outlive the request.** `cleanup()` runs after every
   outcome; the base's `finally` restores the global linecache and nnsight memos
   around your `execute`, which matters only if you deserialize in-process.

## Worked example: the sandbox actor

`SandboxModelDeployment` (`services/ray/sandbox/model.py:191`) is the reference
implementation of exactly this pattern: the model still loads on the host through the
base's `load_from_disk`, but an untrusted request's block runs in a separate runner
process, driven over a socket. It overrides the hooks and nothing else.

It comes in two pieces, and the split is worth copying. `SandboxHost`
(`sandbox/model.py:50`) is a mixin holding everything about *hosting a runner* —
the pool, the payload handshake, the hooks whose sandboxed behaviour is the same
for anybody. `SandboxModelDeployment` mixes it over `BaseModelDeployment` and adds
what one host, owning one whole model, does differently. The other user is the
tensor-parallel actor (`tp/model.py:435`), where each of `tp_size` ranks is a host
to one shared runner.

```python
class SandboxHost:
    def run_in_runner(self, request, seed):
        # `kill` is gated on this: it is how the actor knows a request is in flight.
        self.execution_ident = threading.current_thread().ident
        sandbox = self.pool.acquire()
        self.execution_sandbox = sandbox
        connection = sandbox.connection()
        try:
            self.before_payload(request, sandbox, seed)   # no-op for one host
            connection.send(
                (request.payload, request.compress, str(self.dtype), seed, request.env)
            )
            self.after_payload()                          # no-op for one host
            driver = SandboxDriver(
                self.model,
                self.dtype,
                on_log=lambda text: request.respond(Status.LOG, text),
            )
            return driver.pump(connection)                # (blob, deserialize_ms)
        finally:
            connection.close()

    def execution_scope(self, request):
        # a sandboxed run forwards stdout as PRINT events, so no host redirect
        if request.trusted:
            return super().execution_scope(request)
        return contextlib.nullcontext()

    def format_error(self, exception):
        if isinstance(exception, RunnerError):     # traceback already formatted
            return str(exception), False
        return super().format_error(exception)

    def cleanup(self):
        self.discard_sandbox()                     # fresh process per request
        super().cleanup()


class SandboxModelDeployment(SandboxHost, BaseModelDeployment):
    def __init__(self, *args, pool_size: Optional[int] = None, **kwargs):
        super().__init__(*args, **kwargs)
        # open_pool sets self.pool and self.execution_sandbox. It passes the
        # actor's model_key down so each runner builds its own meta model.
        self.open_pool(pool_size)

    def interrupt(self):
        if self.execution_sandbox is not None:
            self.execution_sandbox.stop()          # unblocks a thread parked on recv
        super().interrupt()

    def execute(self, request):
        if request.trusted:
            return super().execute(request)        # in-process, base path
        return self.run_in_runner(request, None)   # None: one process, so no seed
```

Five things to copy from it:

- **Set `self.execution_ident`** on the worker thread before anything can block
  (here, at the top of `run_in_runner`), so the base's `interrupt()` can reach it.
- **Delegate rather than duplicate.** The trusted branch is `super().execute(request)`
  — one path, not two implementations.
- **Extend, don't replace, `interrupt`/`cleanup`.** Both call `super()` after doing
  their own work.
- **Return an already-serialized blob.** The runner does the `torch.save`
  (`sandbox/nns.py:541`), so the actor hands the bytes back as-is with no
  unpickle/repickle round trip.
- **Keep the host logic out of the actor.** `SandboxDriver` (`sandbox/driver.py:190`)
  drives the model for a runner and knows nothing about Ray or requests, which is
  what lets a tensor-parallel shard — a host with a model and no actor around it —
  use the same code.

## Selecting your actor

`Deployment._resolve_actor_class` (`cluster/deployment.py:100`) accepts either a
dotted import path — imported *inside the Ray worker*, so it must be importable there
— or an already-`@ray.remote` class object. The value is chosen at placement time
(`cluster/cluster.py:290`), from, in order:

| Source | Scope | Where |
|---|---|---|
| `DeploymentConfig.actor_class` | one deploy call | `common/schema/controller.py:68` |
| `NDIF_TP_MODEL_ACTOR_CLASS` | replicas given >1 GPU that shard evenly into exactly that many | `Evaluator.actor_class`, `cluster/evaluator.py:355` |
| `NDIF_MODEL_IMPORT_PATH` | the whole cluster | `ControllerDeploymentArgs`, `controller/controller.py:742` |
| `NDIF_DEFAULT_MODEL_ACTOR_CLASS` | fallback for the above | `controller/controller.py:744` |
| `ndif.services.ray.deployments.modeling.base.ModelActor` | final fallback default | `controller/controller.py:745` |

The controller substitutes its default for any `None` before create, so a `None`
reaching `_resolve_actor_class` is a bug, not a fallback.

Per deployment, from the CLI:

```bash
ndif deploy openai-community/gpt2 --actor-class ndif.services.ray.mystack.model.MyModelActor
```

or in a config file (`ndif deploy -f models.yaml`), per `cli/lib/model_config.py:5`:

```yaml
models:
  - checkpoint: openai-community/gpt2
    replicas: 1
    actor_class: ndif.services.ray.mystack.model.MyModelActor
```

Cluster-wide, set `NDIF_MODEL_IMPORT_PATH` on the ray service. (The compose stack
selects the sandbox actor through the fallback `NDIF_DEFAULT_MODEL_ACTOR_CLASS`
instead — `docker/docker-compose.yml:252` — which resolves to the same thing when
`NDIF_MODEL_IMPORT_PATH` is unset; either variable works, with `NDIF_MODEL_IMPORT_PATH`
taking precedence.)

```yaml
    environment:
      NDIF_MODEL_IMPORT_PATH: ndif.services.ray.mystack.model.MyModelActor
```

Changing the default only affects **new** deployments; the class is captured on each
`Deployment` at placement time, so evict and re-deploy a model to move it.

## Checking it works

Deploying tells you whether the actor constructs and loads: `ndif deploy` blocks on
each new replica's `__ray_ready__` (`cli/lib/deploy.py:177`, waiting in
`cli/lib/models.py:110`), which resolves only after `__init__` — including the weight
load — returns. `ndif status --verbose --json-output` then dumps the controller state,
in which each replica carries its resolved `actor_class` (`Deployment.get_state`,
`deployment.py:130`). From there run
a real `remote=True` trace and check that a terminal status and a presigned url
arrive; see [testing.md](./testing.md).

## Related

- [model-actor.md](./model-actor.md) — what the base class does, in detail.
- [sandbox-internals.md](./sandbox-internals.md) — the worked example, in full.
- [controller-internals.md](./controller-internals.md) — how a `Deployment` becomes an actor.
- [../operating/models-and-deployment.md](../operating/models-and-deployment.md) — deploying, pinning, and evicting models.
- [../reference/env-vars.md](../reference/env-vars.md) — `NDIF_MODEL_IMPORT_PATH`, `NDIF_DEFAULT_MODEL_ACTOR_CLASS` and friends.
