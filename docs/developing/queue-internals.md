---
title: Queue Internals
one_liner: The dispatcher process — one Redis list in, per-model Processors and Replica workers out, plus autoscaling, eviction handling, and the places it can wedge.
tags: [internals, dev, queue, redis, ray]
related: [docs/developing/api-service.md, docs/concepts/queue-and-scheduling.md, docs/developing/controller-internals.md, docs/developing/model-actor.md, docs/reference/redis-keys.md, docs/reference/env-vars.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/services/api/queue/config.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/queue/request_queue.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/common/providers/ray.py, src/ndif/common/redis/events.py]
---

# Queue Internals

## What this covers

`src/ndif/services/api/queue/` — five modules that turn a flat Redis list of
pickled requests into per-model queues serviced by Ray model actors. It lives
under the API package but runs in its own process. The chain is
`Redis queue -> Dispatcher -> Processor -> Replica -> model actor`.

Three facts frame the design:

1. **There is exactly one dispatcher.** It is spawned once by gunicorn's
   `on_starting` hook (`src/ndif/services/api/gunicorn_conf.py`) regardless of
   `NDIF_API_WORKERS`, because all queue state is in-process.
2. **Only the dispatcher holds a Ray connection.** The API workers cannot reach
   the controller or a model actor; the dispatcher is the bridge.
3. **The per-model queues are in memory, not in Redis.** Redis holds one flat
   handoff list — the multi-producer/single-consumer boundary. The moment the
   dispatcher pops a request, that request exists only as a Python object in
   this process.

## Configuration

`src/ndif/services/api/queue/config.py` is a frozen dataclass, `QueueConfig`,
loaded once at import into `CONFIG`. Non-integer or non-positive values raise at
import, so a typo fails the process rather than silently defaulting.

| Field | Env var | Default | What it does |
|---|---|---|---|
| `queue_key` | `NDIF_QUEUE_KEY` | `queue` | The Redis list the API LPUSHes to and the dispatcher BRPOPs from |
| `fetch_timeout_s` | `NDIF_QUEUE_FETCH_TIMEOUT_S` | 10 | BRPOP timeout — bounds how long an idle loop waits before draining errors |
| `fetch_batch_max` | `NDIF_QUEUE_FETCH_BATCH_MAX` | 32 | Max requests drained per dispatch iteration |
| `autoscaling_interval_s` | `NDIF_AUTOSCALING_INTERVAL_S` | 5 | How often a Processor checks its oldest queued request |
| `autoscaling_wait_threshold_s` | `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` | 30 | Oldest-request wait that triggers a scale-up |
| `autoscaling_backoff_s` | `NDIF_AUTOSCALING_BACKOFF_S` | 120 | Pause after a scale-up before re-checking |
| `autoscaling_max_replicas` | `NDIF_AUTOSCALING_MAX_REPLICAS` | 3 | Cap on replicas per model_key added by autoscaling |

Redis and Ray connection settings come from `NDIF_REDIS_URL` and
`NDIF_RAY_ADDRESS` via the providers.

## The Dispatcher

`Dispatcher.start` (`src/ndif/services/api/queue/dispatcher.py`) constructs one
and runs `dispatch_worker()` under `asyncio.run` forever. `__init__` calls
`connect()` *before* the loop exists, so that method is deliberately
synchronous. The module's `__main__` block runs it detached from gunicorn for
debugging — `python -m ndif.services.api.queue.dispatcher`, pointed at the stack
with `NDIF_REDIS_URL` and `NDIF_RAY_ADDRESS`. Never run two: both `BRPOP` the
same list, so each would hold half of every model's queue in its own memory.

`Dispatcher.connect` is the reconnect primitive, used on boot and on every
connection error; it maintains the `ray:connected` flag and clears the cached
status/env (see [The Redis Layer](redis-layer.md#the-rayconnected-flag)).
`RayProvider.connected` (`src/ndif/common/providers/ray.py`) is true only when
the cluster is reachable *and* the `Controller` actor exists.

### The dispatch loop

Each iteration: `get()` a batch, `dispatch()` each request to its Processor,
then `handle_errors()`, which on a connection error purges every Processor and
reconnects.

`Dispatcher.get` blocks on `brpop` for up to `fetch_timeout_s`, then
non-blockingly `rpop`s until the batch is full or the list is empty. The blocking
pop bounds idle latency so `handle_errors` still runs when nothing is arriving;
the batched pops amortize round-trips under load. Both use `async_bytes_client`
because the values are pickles.

`Dispatcher.dispatch` creates a `Processor` per `model_key` on first sight and
never removes one — an idle Processor is a cheap object with an empty pool and a
sleeping autoscaling task.

`Dispatcher.handle_errors` drains the shared `asyncio.Queue` of
`(name, exception)` tuples that Processors and Replicas push onto. Its only real
decision is connection-level: if any error matches
`RayProvider.is_connection_error` (a substring match against
`CONNECTION_ERROR_PATTERNS`) *or* `RayProvider.connected()` is now False, it
purges every Processor with a user-facing message and reconnects. Per-request
errors have already been reported to their users by the Replica; the dispatcher
just logs them.

### The side workers

`dispatch_worker` also starts three long-lived tasks: `status_worker` and
`env_worker`, the refresh half of the coalesced `/status` / `/env` cache, and
`events_worker`, the CLI's control channel (`ndif queue`, `ndif kill`, and the
reconcile event emitted after an out-of-band `ndif deploy` / `ndif evict`). Their
streams, handlers and reply protocol are in
[The Redis Layer](redis-layer.md#the-events-stream). An `ndif kill` of an
executing request goes through `Replica.cancel`: the request is errored and the
dispatch task cancelled, while the model actor runs the block to completion.

## The Processor

One `Processor` per `model_key` (`src/ndif/services/api/queue/processor.py`),
owning a `RequestQueue` (the per-model line), a `Dict[REPLICA_ID, Replica]` pool
sharing it, a `trusted` flag, an autoscaling task created in `__init__` and
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
really waited. Then `ensure_started(request.trusted)` and a QUEUED reply
carrying the request's 1-based position, read back from the queue rather than
assumed to be the depth.

`prepend=True` means "front of my own priority group" and is used only when an
evicted replica returns its in-flight request. A `priority` key does **not**
prepend — it sorts ahead by group.

`Processor.ensure_started` no-ops if any replica exists or setup is already
underway, which is why it is safe to call on every enqueue and on every replica
exit. Otherwise it flips to `PROVISIONING` and fires `start()` as a task. It also
records the request's `trusted` flag when the argument is not `None`; a
re-provision passes `None`, so the first request to deploy a model fixes the
deployment's `trusted` (and so whether a key asking for `trust_remote_code`
could be deployed) — the full path is in
[API Service](api-service.md#trusted-through-the-queue-and-into-the-model-load).

### RequestQueue

The per-model line (`src/ndif/services/api/queue/request_queue.py`). An
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
otherwise `Replica.provision()` deploys exactly one. Either way it goes
`DEPLOYING`, then for each replica: `await replica.wait()`, `replica.start()`,
status `READY`. `reply()` fires after each phase transition so queued clients
see `PROVISIONING` / `DEPLOYING` / `QUEUED` in order.

On any exception the phase-appropriate message is logged with a traceback,
pushed to `error_queue` (so a connection error triggers a dispatcher reconnect),
and — if the Processor never reached READY — passed to `purge()`, which errors
every queued user and clears the pool so nothing hangs. A `DeploymentError` (the
controller refused, and said why) is forwarded to the user as
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

`Processor.purge` errors every queued request, **clears the queue first**, then
cancels every replica — otherwise the cancelled workers' `finally` blocks would
re-provision against requests that were just errored. It ends with
`replicas.clear()` and status `UNINITIALIZED`.

## The Replica

A `Replica` (`src/ndif/services/api/queue/replica.py`) is a
`(model_key, replica_id)` pair plus one `asyncio.Task` pulling from the
Processor's queue. The actor it addresses is named
`{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace, looked up through
`get_model_actor_handle` (`src/ndif/common/providers/ray.py`). The Replica holds
no readiness state beyond its task: `dropped` is
`task is None or task.done()`.

`Replica.provision` calls
`controller.scale.remote(model_key, 1, DeploymentConfig(trusted=...))` —
`scale`, not `deploy`, so the new replica is a copy of one already running (a
sharded model stays sharded); with nothing running it behaves as a bare deploy.
`DeploymentConfig.replicas` is **additive** controller-side, so this always adds
one more, and a controller-reported error raises `DeploymentError`.
`Replica.deploy` is provision + register + wait + start, de-registering on a
setup failure. `Replica.wait` polls the actor's `__ray_ready__` every second,
treating `ValueError` (the controller hasn't created the actor yet) and
`ActorUnavailableError` (the actor exists but is restarting; over Ray Client
`__ray_ready__` raises instead of blocking) as "not yet" and letting anything
else propagate. **There is no timeout on this loop.**

`Replica.worker` loops `while not self.dropped`, awaiting `self.queue.get()` and
dispatching. Its `finally` — which runs even under cancellation — pops this
replica from `processor.replicas` and then either calls `ensure_started()`
(queue non-empty: re-provision) or `mark_idle()` (queue empty: back to
`UNINITIALIZED`).

`Replica.dispatch` sends DISPATCHED, then `await handle.run.remote(request)`.
The failure split is the most consequential logic in this package:

| Caught | Meaning | Result |
|---|---|---|
| `asyncio.CancelledError` | Deliberate cancel — operator kill, or a purge | Error the user, then re-raise to exit the worker. `CancelledError` is a `BaseException`, so without this branch the user would sit on DISPATCHED forever |
| `EVICTED_ERRORS` | `ValueError` (actor lookup failed) / `ActorDiedError` / `CachedActorError` (actor moved to CPU cache, WARM — including a preemption mid-run) | `self.task = None` drops this replica, the request goes back to the **front** of its group via `enqueue(prepend=True)`; the worker loop exits |
| Any other `Exception` | Anything else | Error the user, push to `error_queue`, keep serving |
| …which is an `ActorUnavailableError` | The actor killed itself to clear an unrecoverable CUDA fault (`BaseModelDeployment.restart`) and Ray is respawning the *same* replica (`max_restarts=-1`) | After the above, `await self.wait()` — the worker parks until the replica serves again instead of erroring every queued request for the length of the reload. Nothing is re-queued and the replica is not dropped |

`EVICTED_ERRORS` is matched by type, but **not by a bare `isinstance`** — the
check in `dispatch` also reads the wrapper's `.cause`. `CachedActorError` is
raised inside the actor and arrives wrapped in a `ray.exceptions.RayTaskError`;
the dual RayTaskError-plus-cause class that would satisfy `isinstance` is only
built when `as_instanceof_cause()` is applied, and over Ray Client — how the
dispatcher connects — it is not. A bare isinstance would turn every HOT→WARM
demotion into a user-facing error; the symptom is an actor log showing
`CachedActorError` raised while the dispatcher logs `error_type=RayTaskError`
and takes the generic branch.

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
> `Replica.current_request` — both plain Python objects. Restarting the API
> restarts the dispatcher with it (it is a child of the gunicorn master), so
> `docker compose restart api` drops them all. A client on a blocking websocket
> gets no further status at all — no ERROR, just silence. Only requests still in
> the Redis `queue` list survive.

> **`Replica.wait()` has no timeout.** If the controller reports a successful
> deploy but the actor never becomes ready, `start()` is stuck awaiting it,
> `status` stays `DEPLOYING`, `ensure_started` no-ops forever, and the model's
> queue grows without bound. Check with `ndif queue`: a Processor at `deploying`
> with a rising `queue_size` and no ready replicas.

> **One event loop for all models.** The dispatcher is a single asyncio process,
> so a blocking call anywhere in a Processor or Replica stalls every model's
> queue. Keep this path `await`-only or wrapped in `asyncio.to_thread`. Related:
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
