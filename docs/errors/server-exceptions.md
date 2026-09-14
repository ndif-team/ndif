---
title: Server Exceptions
one_liner: Every exception raised inside the NDIF server — where it is raised, what the user sees, whether it is the user's fault or the server's, and whether it kills a replica.
tags: [errors, internals, ray, sandbox, queue]
related: [docs/errors/client-side-failures.md, docs/developing/model-actor.md, docs/developing/sandbox-internals.md, docs/developing/queue-internals.md, docs/developing/controller-internals.md, docs/concepts/sandbox-execution.md, docs/concepts/status-and-results.md, docs/runbooks/debug-a-stuck-request.md, docs/runbooks/trace-a-users-failed-job.md, docs/gotchas/gpu-and-memory.md]
sources: [src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/sandbox/nns.py, src/ndif/services/ray/sandbox/protocol.py, src/ndif/services/ray/sandbox/host.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/deployments/modeling/util.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/common/providers/ray.py, src/ndif/cli/lib/_common.py]
---

# Server Exceptions

## What this covers

You are reading a log line, not a user's console. This page enumerates the
exceptions NDIF raises in its own processes, where each one comes from, and — the
question that actually decides what you do — **whether it is the user's fault or
the server's**.

Three facts organise everything below:

1. **A user-caused exception is never fatal to a replica.** The model actor
   catches it, formats it, and answers `ERROR`. The actor keeps its weights and
   serves the next request. Only two CUDA messages break that rule.
2. **Tracebacks don't survive cloudpickle.** Any exception that has to cross a
   process boundary — the sandbox socket, the Ray boundary — is formatted to
   *text* at the point it is live and shipped as a string. That is why
   `RunnerError` carries a pre-formatted traceback and why you should never
   expect `__traceback__` to be meaningful after a hop.
3. **The queue distinguishes exactly three failure classes**, by type and not by
   message (`EVICTED_ERRORS`, `queue/replica.py:52`): eviction, deliberate
   cancellation, and everything else. Which bucket an exception lands in decides
   whether the request is retried, errored, or silently re-queued.

## The table

| Exception | Raised at | What the user sees | Cause | Whose fault |
|---|---|---|---|---|
| `RunnerError` | defined `sandbox/model.py:40`, raised `sandbox/model.py:229` | `ERROR` with the block's own traceback | The user's block raised inside the runner process | **user** |
| Any block exception (trusted path) | propagates out of `BaseModelDeployment.execute`, `base.py:379`, caught at `base.py:343` | `ERROR` with the block's own traceback | The user's block raised in the actor process | **user** |
| `OutOfOrderError` | thrown into a worker by `IPCInterleaver.throw`, `sandbox/nns.py:176` | `ERROR`: `'<location>' was requested but the model already ran past it` | A worker was still parked on a location the forward pass never reached | **user** |
| `EarlyStopException` | `sandbox/model.py:140`, `:164`; nnsight's `tracer.stop()` | nothing — the job completes normally | Intentional early stop | **neither** |
| *(no exception)* — timeout | the race in `base.py:298` expires | `ERROR`: `Your job exceeded the execution timeout of {n}s.` | Block ran past `execution_timeout` | **user** (usually) |
| *(no exception)* — kill switch | `base.py:316` | `ERROR`: `Your job was cancelled or preempted by the server.` | `ndif kill`, or an operator cancel | **operator** |
| `CachedActorError` | defined `common/providers/ray.py:25`, raised `base.py:259` | nothing — request is silently re-queued at the front | The actor was demoted to WARM (weights on CPU) between dispatch and run | **server** |
| `ActorDiedError` | Ray, surfaced at `replica.py:231` | nothing — silently re-queued | The actor process died; Ray restarts it (`max_restarts=-1`) | **server** |
| `ValueError` (actor lookup) | `ray.get_actor`, surfaced at `replica.py:231` | nothing — silently re-queued | The named actor no longer exists | **server** |
| `asyncio.CancelledError` | `replica.py:205` | `ERROR`: `Replica was evicted while processing your request...` | The replica's worker task was cancelled — kill, reconcile, or a purge | **operator/server** |
| `ConnectionError` | `sandbox/protocol.py:79` | `ERROR` (host-side traceback) | The runner died or was stopped mid-exchange | **server** (or a deliberate interrupt) |
| `RuntimeError: runner process exited before its socket was ready` | `sandbox/host.py:106` | `ERROR` (host-side traceback) | The runner subprocess crashed at import | **server** |
| `TimeoutError: timed out waiting for the runner socket` | `sandbox/host.py:109` | `ERROR` (host-side traceback) | Runner took >30s to bind its socket | **server** |
| `ModuleNotFoundError` / unpickling errors | inside `RequestModel.deserialize`, called from `base.py:379` or `sandbox/nns.py:487` | `ERROR` with the deserialize traceback | The payload references a module the server doesn't have | **user** |
| `torch.cuda.OutOfMemoryError` (at run) | inside the block, `base.py:379` | `ERROR` with a CUDA OOM traceback | The request's activations exceeded the replica's budget | **user** |
| `torch.cuda.OutOfMemoryError` / `RuntimeError` (at load) | `load_from_disk`, `base.py:144` | `ERROR`: `Error starting model...` | The controller's size estimate was wrong, or the GPU wasn't actually free | **server** |
| `RuntimeError` from `verify_device_placement` | `modeling/util.py:166` | `ERROR`: `Error starting model...` | A weight landed on `meta`, `cpu`, or an unassigned GPU | **server** |
| `NDIFConnectivityError` | defined `cli/lib/_common.py:14`, raised `:45` | CLI: `Cannot connect to Ray at <url>: ...` | Ray or the `Controller` actor is unreachable | **server** |

## User-caused failures

These reach the user as an `ERROR` response and leave the replica healthy.

### RunnerError — the untrusted path's carrier

```python
class RunnerError(Exception):
    """A failure raised by the user's block in the runner, carrying its already-
    formatted traceback (tracebacks don't survive cloudpickle, so the runner
    formats the text and ships that; see ``nns.run``)."""
```

(`src/ndif/services/ray/sandbox/model.py:40`)

The runner catches everything out of the block, formats the traceback **in its
own process** — preferring the worker's `__intervention_tb__` when the failure
came from intervention code, else nnsight's `clean_traceback` — and sends
`("EXCEPTION", text)` (`sandbox/nns.py:508`-`522`). On the host, `next_event`
turns that into `raise RunnerError(text)` (`model.py:229`), and
`SandboxModelDeployment.format_error` (`model.py:188`) returns the string
verbatim with `fatal=False`:

```python
if isinstance(exception, RunnerError):
    return str(exception), False
return super().format_error(exception)
```

So a `RunnerError` in your logs is **always** user code. The corresponding
trusted-path failure has no wrapper class at all: the exception propagates out of
the worker thread, and the base `format_error` (`base.py:444`) strips nnsight
plumbing and the actor's own frames before formatting.

### OutOfOrderError thrown into a dangling worker

After the forward pass returns, `check_dangling` (`model.py:368`) looks for
proxies still holding a park — a worker waiting on a location the model never
reached — and sends `("THROW", id, requester, iteration != 0)` (`model.py:380`).
The runner's `IPCInterleaver.throw` (`nns.py:176`) constructs the error and
throws it into the greenlet so the traceback points at the line that was waiting:

```python
error = OutOfOrderError(
    f"'{requester}' was requested but the model already ran past it"
)
if is_iter:
    try:
        mediator.worker.throw(error)
    except OutOfOrderError:
        warnings.warn(...)
else:
    mediator.worker.throw(error)
```

The `is_iter` branch is the one to remember: an open-ended `tracer.iter` that
outran the model **warns instead of failing**, and the iterations that were
reached are kept. Anything else fails the run. This mirrors nnsight's local
`Interleaver.check_dangling_mediators` exactly — the same class, the same
message — so the user's fix is the same one nnsight's own docs give: access
modules in forward-pass order, or bound the iteration.

### EarlyStopException — not an error

`tracer.stop()` raises `EarlyStopException` in a worker. Over the socket the
runner's `pump` sends `("STOP",)` and re-raises (`nns.py:143`); on the host both
`MediatorProxy.switch` (`model.py:164`) and `settle_control` (`model.py:140`)
raise it to unwind the forward pass, which the model's `Interleaver.__exit__`
swallows, and `IPCEnvoy.interleave` swallows it in the runner too (`nns.py:230`).
The job proceeds to upload its saves and completes. Seeing this in a stack trace
is normal.

### Deserialization failures

`RequestModel.deserialize` recompiles the block from its shipped **source** and
unpickles the globals and locals it referenced. Anything the payload names has to
exist on the server side:

| Failure | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError` | The block references a package installed on the client but not the server | nnsight's `pull_env()` registers *local, non-installed* modules for pickling by value, so this is normally an installed third-party package. Compare with `ndif env` vs `ndif env --local` |
| `AttributeError` / `UnpicklingError` on a class | Client and server disagree on a type's shape | An nnsight version mismatch — see [Client/server versions](../gotchas/client-server-versions.md) |
| `ModuleNotFoundError: nnsight._c` | The optional C extension didn't compile in the image | `value.save()` breaks server-side; the Dockerfile installs `gcc`/`libc6-dev` specifically to avoid this |

`deserialize_ms` is measured separately on both paths, so the execution-time
metric tells you whether a slow request was deserialize-bound.

## Server-caused failures

### CachedActorError and the eviction bucket

```python
if self.cached:
    raise CachedActorError(f"Model actor {self.model_key} is cached (WARM).")
```

(`base.py:259`) — raised **before** `run()`'s `try` block, deliberately, so it
propagates as an eviction rather than being converted into a user-facing `ERROR`.
It crosses the Ray boundary wrapped in a `RayTaskError` whose dynamic subclass
still satisfies `isinstance` (`common/providers/ray.py:25`).

`Replica.dispatch` groups it with the other two eviction signals
(`replica.py:52`):

```python
EVICTED_ERRORS = (ValueError, ActorDiedError, CachedActorError)
```

On any of them (`replica.py:231`) the replica sets `self.task = None` — ending
its own worker loop — and hands the in-flight request back to the **front** of
the Processor's queue via `enqueue(prepend=True)`. The user is not told anything;
they see `QUEUED` a second time. `enqueue` stamps `enqueued_at` only if unset, so
the autoscaler still sees the original wait.

> **Gotcha:** an eviction mid-flight loses the work done so far. There is no
> checkpoint — the request is re-run from the start on whichever replica picks it
> up next. A model flapping HOT↔WARM makes requests loop without ever finishing.

### asyncio.CancelledError

`CancelledError` inherits from `BaseException`, so a plain `except Exception`
would not catch it. `Replica.dispatch` has a dedicated branch (`replica.py:205`)
that answers the user before re-raising:

```
Replica was evicted while processing your request. Sorry for the inconvenience.
Please try again later.
```

Without that branch the user would sit on `DISPATCHED` forever. The three things
that cancel a worker task are `ndif kill` on an executing request,
`Processor.reconcile` dropping a replica the controller no longer lists, and
`Processor.purge` during a dispatcher reconnect.

### The sandbox transport errors

All three come from the host talking to a runner process:

| Exception | Raised at | Meaning |
|---|---|---|
| `ConnectionError("connection closed before message complete")` | `protocol.py:79` | `recvn` got a short read — the runner died, or `interrupt()` stopped it |
| `RuntimeError("runner process exited before its socket was ready")` | `host.py:106` | The runner crashed during import. Check the actor's stderr; `spawn` runs it with `stdout`/`stderr` to `DEVNULL` by default (`host.py:86`), so run with `quiet=False` to see it |
| `TimeoutError("timed out waiting for the runner socket")` | `host.py:109` | 30s elapsed without the socket accepting — usually a very slow import or a full `/tmp` |

A `ConnectionError` during a *timeout or cancel* is expected: `interrupt`
(`model.py:201`) stops the runner precisely to unblock the host thread parked in
`recv`. One that appears with no timeout is a real runner crash.

### Failures at model load

Load happens in the actor's `__init__` (`load_from_disk`, `base.py:144`), so the
actor never becomes `__ray_ready__` and the failure surfaces to the *queue*, not
to a request. The Processor's `start()` catches it and purges, so every queued
user for that model gets `Error starting model. Please try again later.`
(`processor.py:180`). The real exception is in the API log and the Ray dashboard's
actor log.

| Failure | Meaning |
|---|---|
| `torch.cuda.OutOfMemoryError` at load | The controller's padded size estimate was too small, or a previous actor is still holding memory the controller believes is free |
| `RuntimeError: '<name>' is still on the meta device` | accelerate didn't dispatch — a `device_map`/`max_memory` problem (`util.py:166`) |
| `RuntimeError: '<name>' is on cuda:N, outside the assigned set` | The actor placed weights on a card it wasn't granted |
| `RuntimeError: No tensors were placed on assigned GPUs [...]` | A granted card holds nothing; the allocation is wrong |
| An HF error about `trust_remote_code` | The deployment is untrusted and the repo ships custom modelling code |

> **Gotcha:** `NDIF_MODEL_CACHE_PERCENTAGE` (default 0.9) is **not** the lever for
> a GPU OOM. It scales the fraction of a node's **CPU** RAM usable as WARM cache
> (`services/ray/resources.py`, `controller.py:547`). For GPU pressure the knobs
> are `NDIF_DEFAULT_PADDING_FACTOR` / `NDIF_DEFAULT_PADDING_BIAS`, an explicit
> evict, or `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS`.

### OOM at load vs OOM at run

Same exception class, completely different handling:

| | At load | At run |
|---|---|---|
| Where | `__init__` → `load_from_disk` (`base.py:144`) | inside the block (`base.py:379`) |
| Who sees it | the queue: `Error starting model...` to every queued user | one user, with a CUDA traceback |
| Replica after | never became ready; the controller kills it and releases its accounted bytes (`_monitor_deployment`, `controller.py:298`) | still healthy, still serving |
| Fix | sizing/placement — [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) | the user's batch size, sequence length, or number of saved activations |

### The two fatal CUDA errors

`format_error` returns a `fatal` flag, set by matching the message against two
strings (`base.py:70`):

```python
_UNRECOVERABLE_CUDA_ERRORS = (
    "device-side assert triggered",
    "an illegal memory access was encountered",
)
```

These poison the process's CUDA context permanently — every subsequent CUDA op
raises — so `run()`'s `finally` calls `restart()` (`base.py:511`), which is
`ray.kill(current_actor, no_restart=False)`. `max_restarts=-1` brings the replica
back and it reloads the weights, costing every other user of that model a load
cycle. The *triggering* request is still a user error and still gets its
traceback; the collateral damage is what makes this the one user-caused failure
that is fatal.

### NDIFConnectivityError

Operator-facing only — it lives in the CLI's shared helpers
(`cli/lib/_common.py:14`) and is raised by `ensure_ray_connected` (`:45`) after
one reset-and-reconnect attempt fails. The dashboard imports the same functions,
so its deploy/evict routes catch it too (`backend/routers/deploy.py:94`) and its
reconcile job downgrades it from `exception` to `error` level
(`jobs/reconcile.py:254`) because an unreachable cluster is expected, not a bug.

It means one of three things, all checked by `RayProvider.connected()`
(`common/providers/ray.py:90`): Ray isn't initialized, the address isn't
listening, or the `Controller` actor isn't resolvable in the `NDIF` namespace.
The third is the one people miss — Ray can be perfectly healthy while the
controller is gone.

## How the dispatcher classifies errors

Processors and Replicas push `(name, exception)` onto a shared `error_queue`;
`Dispatcher.handle_errors` (`dispatcher.py:165`) drains it. Its only real
decision is connection-level: if any error matches
`RayProvider.is_connection_error` — a substring match against
`CONNECTION_ERROR_PATTERNS` (`common/providers/ray.py:108`), which lists gRPC and
Ray-client disconnect strings — or `RayProvider.connected()` is now False, it
purges every Processor with a user-facing message and reconnects.

Everything else is logged and dropped, because the Replica has already told the
user. That is why an isolated `request errored during execution` line in the
dispatcher log needs no action: the user got their traceback.

## Related

- [Client-side failures](client-side-failures.md) — the same events from the
  user's console.
- [Model actor](../developing/model-actor.md) — the `run()` template these
  exceptions are raised inside, and the metrics each outcome emits.
- [Sandbox internals](../developing/sandbox-internals.md) — the socket protocol,
  `RunnerError`'s round trip, and the termination table.
- [Queue internals](../developing/queue-internals.md) — `EVICTED_ERRORS`, purge,
  and the reconnect loop.
- [Debug a stuck request](../runbooks/debug-a-stuck-request.md) — when nothing
  raised at all.
