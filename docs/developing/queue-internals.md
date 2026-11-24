---
title: Queue Internals
one_liner: The dispatcher actor — one Redis list in, per-model Processors and Replica workers out, plus autoscaling, eviction handling, and the places it can wedge.
tags: [internals, dev, queue, redis, ray]
related: [docs/developing/api-service.md, docs/concepts/queue-and-scheduling.md, docs/developing/controller-internals.md, docs/developing/model-actor.md, docs/reference/redis-keys.md, docs/reference/env-vars.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/services/ray/queue/config.py, src/ndif/services/ray/queue/dispatcher.py, src/ndif/services/ray/queue/processor.py, src/ndif/services/ray/queue/replica.py, src/ndif/services/ray/queue/request_queue.py, src/ndif/services/ray/queue/runtime_limit.py, src/ndif/services/ray/start.sh, src/ndif/common/providers/ray.py, src/ndif/cli/lib/dispatcher.py]
---

# Queue Internals

## What this covers

`src/ndif/services/ray/queue/` — five modules that turn a flat Redis list of
pickled requests into per-model queues serviced by Ray model actors. It runs
inside the Ray cluster, in one detached actor on the head node. The chain is
`Redis queue -> Dispatcher -> Processor -> Replica -> model actor`.

Three facts frame the design:

1. **There is exactly one dispatcher.** It is the detached Ray actor
   `Dispatcher` in the `NDIF` namespace, launched once on the head by
   `src/ndif/services/ray/start.sh`, because all queue state is in-process.
2. **The API never touches Ray.** Its workers cannot reach the controller or a
   model actor; the dispatcher is the bridge, and Redis is its only link to
   them.
3. **The per-model queues are in memory, not in Redis.** Redis holds one flat
   handoff list — the multi-producer/single-consumer boundary. The moment the
   dispatcher pops a request, that request exists only as a Python object in
   this process.

## Configuration

`src/ndif/services/ray/queue/config.py` is a frozen dataclass, `QueueConfig`,
loaded once at import into `CONFIG`. Non-integer or non-positive values raise at
import, so a typo fails the process rather than silently defaulting.

| Field | Env var | Default | What it does |
|---|---|---|---|
| `queue_key` | `NDIF_QUEUE_KEY` | `queue` | The Redis list the API LPUSHes to and the dispatcher BRPOPs from |
| `fetch_timeout_s` | `NDIF_QUEUE_FETCH_TIMEOUT_S` | 10 | BRPOP timeout — the length of an idle pass of the dispatch loop. The stream workers' `xread` block (`STREAM_BLOCK_MS`) and the dispatcher's Redis reply timeout (it plus 10 s) derive from it |
| `autoscaling_interval_s` | `NDIF_AUTOSCALING_INTERVAL_S` | 5 | How often a Processor checks its oldest queued request |
| `autoscaling_wait_threshold_s` | `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` | 30 | Oldest-request wait that triggers a scale-up |
| `autoscaling_backoff_s` | `NDIF_AUTOSCALING_BACKOFF_S` | 120 | Pause after a scale-up before re-checking |
| `autoscaling_max_replicas` | `NDIF_AUTOSCALING_MAX_REPLICAS` | 3 | Cap on replicas per model_key added by autoscaling |

The Redis connection comes from `NDIF_REDIS_URL` via the provider. The
dispatcher reads `CONFIG` in its own process, an actor's worker on the head, so
these are set in the environment of the ray service's head node. The API reads
only `queue_key`.

## The Dispatcher

`DispatcherActor` (`src/ndif/services/ray/queue/dispatcher.py`) is the actor:
named `Dispatcher`, pinned to the head by `resources={"head": 1}`, with
`num_cpus=0` and `max_restarts=-1`. `app()` in that module launches it detached
with `get_if_exists=True`, so a relaunch reuses a live one, and
`src/ndif/services/ray/start.sh` runs
`python -m ndif.services.ray.queue.dispatcher` on the head right after the
Controller launch. Its `__init__` connects the telemetry sinks, puts a reply
timeout on the async Redis clients ([Providers](providers.md#redisprovider)),
and starts `Dispatcher.dispatch_worker` as a task, so when Ray restarts the
actor the loop starts again, without the requests the previous one had popped.
Never run two against one Redis: both `BRPOP` the same list, so each would hold
half of every model's queue in its own memory.

It reaches the Controller and the model actors through Ray's own handles
(`controller_handle`, `model_handle` in `src/ndif/common/providers/ray.py`).
`Dispatcher.startup` runs once before the loop: it waits for Redis and deletes
the cached status/env and their coalescing locks. `Dispatcher.heartbeat_worker` keeps the `ray:connected`
key alive from then on (see
[The Redis Layer](redis-layer.md#the-rayconnected-flag)).

### The dispatch loop

Each iteration: `get()` a request, then `dispatch()` it to its Processor. An
error in the loop is logged and the loop goes on a second later.

`Dispatcher.get` blocks on `brpop` for up to `fetch_timeout_s` and returns one
request, or `None` when none arrived. It uses `async_bytes_client` because the
values are pickles.

`Dispatcher.dispatch` creates a `Processor` per `model_key` on first sight and
never removes one — an idle Processor is a cheap object with an empty pool and a
sleeping autoscaling task.

### The side workers

`dispatch_worker` also starts three long-lived tasks: `heartbeat_worker`, which
renews `ray:connected`; and `status_worker` and `env_worker`, the refresh half
of the coalesced `/status` / `/env` cache, whose streams are in
[The Redis Layer](redis-layer.md#the-coalesced-cache). Nothing restarts these tasks, so
each one, like the main loop, catches every error, logs it and retries a second
later, which also carries them through a Redis outage. The stream reads block
for `STREAM_BLOCK_MS` at a time, not forever, so one on a connection that died
without closing times out and is retried.

`dispatch_worker` starts `state_worker` too, which writes each model's
`dispatcher_state` point every 30 s ([Telemetry Internals](telemetry-internals.md#metrics)).

### Calls from the CLI

Three methods on the actor are for callers outside it. The CLI and the dashboard
reach them the way they reach the Controller: over Ray Client, through the
handle `get_dispatcher_actor_handle()` returns
(`src/ndif/common/providers/ray.py`), from `src/ndif/cli/lib/dispatcher.py`. They
run on the actor's event loop, between passes of the dispatch loop, and act on
what only the dispatcher holds: its Processors.

| Method | Caller | Does | Returns |
|---|---|---|---|
| `queue_state()` | `ndif queue` | Snapshots every Processor (`Processor.snapshot`) | `{"processors": {model_key: snapshot}}` |
| `kill(request_id)` | `ndif kill` | Removes and errors a queued request; errors and interrupts an executing one | `{"status", "message"}`, with `status` one of `removed_from_queue`, `cancelled_execution`, `not_found` |
| `reconcile(model_key)` | the end of `ndif deploy` / `ndif evict`, and the dashboard's same calls | Re-syncs that model's Processor with the Controller's replica list (`Processor.reconcile`); nothing for a model with no Processor | `None`; the caller does not wait |

An exception in one reaches its caller as a `RayTaskError`. A kill of an
executing request goes through `Replica.kill`: the request is errored, and the
actor's `cancel()` is called every 100 ms until the dispatch returns, for a few
seconds at most. The actor ignores a cancel that reaches it before the block
has begun. The replica's worker task is never cancelled, so a kill cannot cost
the pool a replica.

## The Processor

One `Processor` per `model_key` (`src/ndif/services/ray/queue/processor.py`),
owning a `RequestQueue` (the per-model line), a `Dict[REPLICA_ID, Replica]` pool
sharing it, an autoscaling task created in `__init__` and
never cancelled, and a `status` (`ProcessorStatus`): `UNINITIALIZED` /
`PROVISIONING` / `DEPLOYING` / `READY` / `CANCELLED`. There is no BUSY —
busy-ness lives per replica, and a Processor is READY whenever at least one
replica serves.

It is lazy and self-healing, with no teardown: an idle Processor sits with an
empty pool and is re-provisioned on the next request.

### enqueue

`Processor.enqueue` hands the request to `RequestQueue.put`, which stamps
`enqueued_at` **only if unset** — so a request handed back after an eviction
keeps its original timestamp and the autoscaler still sees how long it has
really waited. Then `ensure_started()` and a QUEUED reply
carrying the request's 1-based position, read back from the queue rather than
assumed to be the depth.

`prepend=True` means "front of my own priority group" and is used only when an
evicted replica returns its in-flight request. A `priority` key does **not**
prepend — it sorts ahead by group.

`Processor.ensure_started` no-ops if any replica exists or setup is already
underway, which is why it is safe to call on every enqueue and on every replica
exit. Otherwise it flips to `PROVISIONING` and fires `start()` as a task.

### RequestQueue

The per-model line (`src/ndif/services/ray/queue/request_queue.py`). An
`asyncio.PriorityQueue` keyed `(rank, enqueued_at, seq)`:

```
rank 0  priority, re-queued      rank 2  normal, re-queued
rank 1  priority                 rank 3  normal
```

The group is doubled so `prepend` is a sub-rank inside it — a plain "+1" would
collide a re-queued normal request with a fresh priority one. Every tier
tiebreaks on `enqueued_at`, so each is FIFO and nothing starves *within* a group.
`seq` is a monotonic counter that stops tuple comparison before it reaches the
`BackendRequestModel`, which is not orderable.

Ordering is a key rather than an insertion side because pushing priority
requests onto the *front* of a plain FIFO makes the priority group LIFO, which
starves under load: a closed-loop client that wins the head keeps winning it.

Callers go through the class rather than touching `_queue`: a heap's list is
only partially sorted, so reading it raw gives a plausible-looking wrong order.

| Method | For |
|---|---|
| `put(request, prepend=False)` | enqueue; stamps `enqueued_at` if unset |
| `get()` | await the next request to serve |
| `snapshot()` | queued requests in true service order (position replies, status) |
| `oldest()` | longest-waiting request across *both* groups — what autoscaling reads |
| `position(id)` | 1-based place in the service order |
| `remove(id)` | pull one out by id, re-heapifying |

`oldest()` is an O(n) scan, once per autoscaling tick, rather than a maintained
watermark.

**No aging.** Sustained priority traffic starves normal traffic indefinitely —
autoscaling is the only relief and it caps at `autoscaling_max_replicas`.

### start

`Processor.start` asks the controller for the model's current replicas
(`get_deployment`). If any exist it adopts **all** of them as `Replica` objects;
otherwise `Replica.provision()` deploys exactly one — unless the key carries
`trust_remote_code` and no queued request has `request.trust_remote_code`, which
raises `DeploymentError` ([API Service](api-service.md#who-may-deploy-a-trust_remote_code-key)). Either way it goes
`DEPLOYING`, then for each replica: `await replica.wait()`, `replica.start()`,
status `READY`. `reply()` fires after each phase transition so queued clients
see `PROVISIONING` / `DEPLOYING` / `QUEUED` in order.

On any exception the phase-appropriate message is logged with a traceback
and — if the Processor never reached READY — passed to `purge()`, which errors
every queued user and clears the pool so nothing hangs. A `DeploymentError` (the
controller refused and said why, or `start` refused the key) is forwarded to the user as
`Could not deploy this model. <reason>`, truncated at 1500 characters; any other
exception keeps the generic message.

`Processor.reply` with `request=None` broadcasts to every queued request,
annotating each with its position (`"Moved to position N in Queue."`). It
publishes one Redis message per queued request, so a deep queue means a burst of
pubsub traffic on every phase change.

### Autoscaling

`Processor.autoscaling_loop` is a single long-lived task per Processor:

```python
while self.status != ProcessorStatus.CANCELLED:
    if self.status == ProcessorStatus.READY:
        oldest = self.queue.oldest()
        if oldest is not None and oldest.enqueued_at is not None:
            wait = time.time() - oldest.enqueued_at
            if (wait > CONFIG.autoscaling_wait_threshold_s
                    and len(self.replicas) < CONFIG.autoscaling_max_replicas):
                await self.scale_up(wait)
                await asyncio.sleep(CONFIG.autoscaling_backoff_s)
                continue
    await asyncio.sleep(CONFIG.autoscaling_interval_s)
```

The decision is entirely **oldest-request wait** — not queue depth, not
throughput. A hundred requests that all arrived a second ago will not scale up;
one request that has waited 31 seconds will. It reads the oldest request across
both groups, not the head, because under priority traffic the head is the newest
priority request and its wait says nothing about starving normal requests. It
only fires in `READY` (bringing up the *first* replica is `start()`'s job) and
stops at `autoscaling_max_replicas`. Each tick is individually guarded so one
transient error can't kill the task.

`Processor.scale_up` calls `Replica.deploy`, which registers the new replica in
the pool *before* starting its worker so it counts against the cap while coming
up, then waits for readiness. That `await` blocks the autoscaling loop for the
whole model load, which the backoff that follows absorbs anyway.

There is no scale-*down* here. Shrinking is eviction, driven by the controller
(see [Controller Internals](controller-internals.md)), and reaches the queue via
`reconcile` or an `EVICTED_ERRORS` dispatch failure.

### reconcile and purge

`Processor.reconcile` re-reads the controller's replica list. It **adopts** what
the controller has gained — registered, then waited on and started by `adopt()`
in a background task — and deliberately **does nothing** about what the
controller has dropped beyond logging it.

Not shedding is the point. `Replica.cancel` errors the in-flight request, so
shedding here would kill a request that is blameless and still runnable — the
common case being an eviction that demotes a replica to WARM. The worker handles
a vanished replica by itself: the next dispatch to it raises one of
`EVICTED_ERRORS` (`CachedActorError` when demoted, `ValueError`/`ActorDiedError`
when removed), `dispatch` hands the request back to the *front* of the queue,
sets `task = None`, and the worker's `finally` drops the replica and
re-provisions. The cost is an idle replica lingering in the pool until the next
request pays one wasted dispatch.

Adoption is the only path that picks up a replica while the model is already
serving: `ensure_started` no-ops on a non-empty pool and `start` only runs on an
empty one. It is what makes an out-of-band `ndif deploy` of a second replica
contribute capacity to a busy model without restarting the dispatcher.

Two details to keep if you touch this. `adopt` runs as a task rather than inline
because `reconcile` is *awaited* by the events worker and `Replica.wait` has no
timeout — waiting inline would wedge that worker, and every later reconcile and
`ndif kill`, on one unready actor. And adoption is skipped while `status` is
`PROVISIONING`/`DEPLOYING`, because a `start()` already in flight adopts the same
list and the two would race into two workers on one replica.

`Processor.purge`, called only by `start` on a failed provision, errors every
queued request, **clears the queue first**, then cancels every replica — otherwise the cancelled workers' `finally` blocks would
re-provision against requests that were just errored. It ends with
`replicas.clear()` and status `UNINITIALIZED`.

## The Replica

A `Replica` (`src/ndif/services/ray/queue/replica.py`) is a
`(model_key, replica_id)` pair plus one `asyncio.Task` pulling from the
Processor's queue. The actor it addresses is named
`{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace, looked up through
`model_handle` (`src/ndif/common/providers/ray.py`). The Replica holds
no readiness state beyond its task: `dropped` is
`task is None or task.done()`.

`Replica.provision` calls
`controller.scale.remote(model_key, 1, DeploymentConfig())` —
`scale`, not `deploy`, so the new replica is a copy of one already running (a
sharded model stays sharded); with nothing running it behaves as a bare deploy.
`DeploymentConfig.replicas` is **additive** controller-side, so this always adds
one more, and a controller-reported error raises `DeploymentError`.
`Replica.deploy` is provision + register + wait + start, de-registering on a
setup failure. `Replica.wait` awaits the actor's `__ray_ready__`, polling every
second on `ActorUnavailableError` (the actor is restarting, and a call made
while it is can raise at once instead of waiting out the restart) and letting
anything else propagate.
That includes `ValueError` from the lookup: a name is registered when the actor is
created, before `deploy` returns, and stays registered through restarts, so a
failed lookup means the replica is gone. **There is no timeout on this loop.**

`Replica.worker` loops `while not self.dropped`, awaiting `self.queue.get()` and
dispatching. Its `finally` — which runs even under cancellation — pops this
replica from `processor.replicas` and then either calls `ensure_started()`
(queue non-empty: re-provision) or `mark_idle()` (queue empty: back to
`UNINITIALIZED`).

`Replica.dispatch` sends DISPATCHED, then `await handle.run.remote(request)`.
Around that it applies the
[runtime limit](../concepts/auth-and-limits.md#runtime-limit): a request whose
user is out of runtime is errored before DISPATCHED, and a run the actor returns
from is charged to its user (`src/ndif/services/ray/queue/runtime_limit.py`).
The failure split is the most consequential logic in this package:

| Caught | Meaning | Result |
|---|---|---|
| `asyncio.CancelledError` | Deliberate cancel — operator kill, or a purge | Error the user, then re-raise to exit the worker. `CancelledError` is a `BaseException`, so without this branch the user would sit on DISPATCHED forever |
| `EVICTED_ERRORS` | `ValueError` (actor lookup failed) / `ActorDiedError` / `CachedActorError` (actor moved to CPU cache, WARM — including a preemption mid-run) | `self.task = None` drops this replica, the request goes back to the **front** of its group via `enqueue(prepend=True)`; the worker loop exits |
| Any other `Exception` | Anything else | Error the user, log it, keep serving |
| …which is an `ActorUnavailableError` | The actor killed itself to clear an unrecoverable CUDA fault (`BaseModelDeployment.restart`) and Ray is respawning the *same* replica (`max_restarts=-1`) | After the above, `await self.wait()` — the worker parks until the replica serves again instead of erroring every queued request for the length of the reload. Nothing is re-queued and the replica is not dropped |

`EVICTED_ERRORS` is matched by a plain `isinstance`. `CachedActorError` is
raised inside the actor and reaches `dispatch` as
`RayTaskError(CachedActorError)`, an instance of both classes, because the
dispatcher is inside the cluster. Over Ray Client — the CLI and the dashboard —
the wrapper arrives plain, `isinstance` against the original type matches
nothing, and the cause has to be read off `.cause`.

`Replica.cancel` errors the in-flight request *before* cancelling the task,
because a teardown only notifies queued requests via `Processor.reply` — the
running one would otherwise hang. Cancelling the task stops the dispatcher's
await on `handle.run.remote`; it does not stop the model actor.

## Redis data structures

The queue uses one Redis list (`queue`, `NDIF_QUEUE_KEY`, pickled
`BackendRequestModel`s) plus the keys in [The Redis Layer](redis-layer.md);
every key is in [Redis Keys](../reference/redis-keys.md). What is **not** in
Redis: the per-model queues, the replica pool, and every request between the
`BRPOP` and its terminal response. Those are Python objects in the dispatcher's
heap.

## Where it wedges

> **A dispatcher restart loses every in-flight and queued request.** Once
> popped, a request waits in a `Processor.queue` or sits in
> `Replica.current_request` — both plain Python objects in the actor's process.
> Whether the actor dies alone (Ray restarts it, and the new one adopts the
> running replicas through `Processor.start` as requests arrive) or with the
> head, nothing errors the requests it held. Only requests still in the Redis
> `queue` list survive.

> **`Replica.wait()` has no timeout.** If the controller reports a successful
> deploy but the actor never becomes ready, `start()` is stuck awaiting it,
> `status` stays `DEPLOYING`, `ensure_started` no-ops forever, and the model's
> queue grows without bound. Check with `ndif queue`: a Processor at `deploying`
> with a rising `queue_size` and no ready replicas.

> **One event loop for all models.** The dispatcher is a single asyncio loop,
> so a blocking call anywhere in a Processor or Replica stalls every model's
> queue, and `heartbeat_worker` with it: `ray:connected` lapses and the API
> answers 503.
> Keep this path `await`-only or wrapped in `asyncio.to_thread`. Ray calls go
> through `controller_handle()` and `model_handle()`, Ray's own handles, whose
> `await handle.method.remote(...)` yields to the loop (see
> [Providers](providers.md#rayprovider)). Related:
> `ensure_started` keys off the pool being empty, so a Processor with one wedged
> replica will not provision a second no matter how deep its queue gets —
> autoscaling requires `READY`, `ensure_started` requires an empty pool.
> Deploying one out-of-band is the way out: `reconcile` adopts it.

> **Autoscaling has no GPU-capacity view.** `Replica.provision` just asks the
> controller for one more replica; the controller decides whether that means
> evicting something else. A scale-up can therefore evict a *different* model,
> whose replicas then fail their next dispatch with `EVICTED_ERRORS` and
> re-provision — a thrash loop between two models both under pressure.

> **A private asyncio API.** `RequestQueue` reaches into
> `asyncio.PriorityQueue._queue` — the heap's backing list — in `snapshot`,
> `oldest`, `position`, `remove` and `clear`, because there is no public way to
> read or mutate a queue's contents without consuming them. A CPython change to
> `asyncio.Queue` internals breaks the queue subsystem.

## Related

- [API Service](api-service.md) — where requests come from, and where `trusted`
  is decided.
- [Queue and Scheduling](../concepts/queue-and-scheduling.md) — why the queue is
  shaped like this.
- [Controller Internals](controller-internals.md) — the other side of `scale` /
  `get_deployment` / `status` / `env`.
- [Model Actor](model-actor.md) — what `handle.run.remote(request)` does.
- [Debug a Stuck Request](../runbooks/debug-a-stuck-request.md) — `ndif queue`,
  `ndif kill`.
- [Env Vars](../reference/env-vars.md), [Redis Keys](../reference/redis-keys.md).
