---
title: Queue and Scheduling
one_liner: One Redis list feeding per-model in-memory queues inside a single dispatcher process — how a request finds its model, when a second replica appears, and what "fair" does and doesn't mean here.
tags: [concepts, queue, redis, api]
related: [docs/concepts/request-lifecycle.md, docs/concepts/deployments-and-eviction.md, docs/concepts/status-and-results.md, docs/developing/queue-internals.md, docs/reference/env-vars.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/services/api/queue/config.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/app.py, src/ndif/services/api/gunicorn_conf.py]
---

# Queue and Scheduling

## What this covers

The queueing model: where requests actually wait, how one is routed to a model,
what causes a new replica to appear, and what the scheduler does not do.

Two facts frame it:

1. **There are two queues, not one.** A single Redis list is the boundary
   between the API's *N* gunicorn workers and the *one* dispatcher process. Past
   that boundary, every request lives in an in-memory `asyncio.Queue` owned by a
   `Processor` — one per `model_key`. The Redis list is the multi-producer /
   single-consumer handoff; the per-model queues are where waiting actually
   happens.
2. **Scheduling is FIFO with one escape hatch.** There is no weighting, no
   round-robin between users, no aging, and no fairness across models beyond
   "each model drains its own line". A `priority` API key jumps to the front;
   everything else is arrival order.

## The shape

```
api worker 1 ─┐
api worker 2 ─┼─ LPUSH "queue" ─→ Redis list ─→ BRPOP ─→ Dispatcher
api worker N ─┘                                            │
                                          routes by model_key
                                            ┌──────────────┴──────────────┐
                                     Processor(gpt2)            Processor(llama-70b)
                                     asyncio.Queue              asyncio.Queue
                                     Replica ×1..3              Replica ×1..3
                                        │                          │
                                   run.remote()               run.remote()
                                   ModelActor                 ModelActor
```

The dispatcher is not a container — it is a process the API's gunicorn master
spawns at startup (`src/ndif/services/api/gunicorn_conf.py`, `on_starting`).
It is deliberately *spawned*, not forked, so the telemetry providers' background
threads are created fresh in it.

## Enqueue and drain

The API pickles the whole `BackendRequestModel` — payload included — and
`LPUSH`es it onto the list named by `NDIF_QUEUE_KEY`
(`src/ndif/services/api/app.py:180`). The dispatcher `BRPOP`s the other end,
which makes it FIFO, and then drains up to `NDIF_QUEUE_FETCH_BATCH_MAX - 1` more
with non-blocking `RPOP` to amortize round-trips
(`src/ndif/services/api/queue/dispatcher.py:112`):

```python
result = await client.brpop(CONFIG.queue_key, timeout=CONFIG.fetch_timeout_s)
if result is None:
    return []
requests = [pickle.loads(result[1])]
while len(requests) < CONFIG.fetch_batch_max:
    item = await client.rpop(CONFIG.queue_key)
    ...
```

The `fetch_timeout_s` bound exists so an idle loop still wakes periodically to
drain the error queue and notice a broken Ray connection.

> **Gotcha:** once a request is popped it lives only in the dispatcher's memory.
> Restart the API container and every queued-but-not-yet-running request is gone
> — the client keeps waiting on a websocket that will never see another update.
> Requests still sitting in the Redis list survive.

## How a request finds its model

The target is `request.model_key`, chosen entirely by the client: nnsight's
`to_model_key()` produces `"<import.path.ClassName>:<model id>"` (e.g.
`nnsight.modeling.transformers.TransformersModel:openai-community/gpt2`). The
API never validates it — it is just a dict key:

```python
if request.model_key not in self.processors:
    self.processors[request.model_key] = Processor(request.model_key, self.error_queue)
await self.processors[request.model_key].enqueue(request, prepend=request.priority)
```

(`src/ndif/services/api/queue/dispatcher.py:135`)

So a typo'd or unavailable model key creates a real `Processor`, which asks the
controller to deploy it, which fails in the evaluator — and the user gets an
`ERROR` after a provisioning attempt, not a fast rejection. There is no
allow-list of deployable models on the request path.

A `Processor` is never torn down. An idle one sits with an empty pool at
`UNINITIALIZED` and is reused when the next request for that model arrives.

## Provisioning: the first replica

`enqueue` calls `ensure_started`, which no-ops if a replica already exists or
setup is already underway (`src/ndif/services/api/queue/processor.py:140`).
Otherwise `start()` asks the controller for the model's current replicas; it
adopts whatever is listed, or asks for one new one via `Replica.provision`. The
client sees this as `PROVISIONING` then `DEPLOYING`, and `QUEUED` with a
position once the pool is serving.

Each `Replica` runs a worker task that pulls from the *shared* per-model queue,
so replicas of the same model compete for the same line — first free replica
takes the head. Nothing partitions requests across replicas in advance.

> **Gotcha:** the `Processor` records `trusted` from the request that first
> provisions it (`Processor.ensure_started`,
> `src/ndif/services/api/queue/processor.py:140`) and passes it into the
> `DeploymentConfig` it sends the controller — which becomes the deployment's
> `trust_remote_code`. A re-provision passes `None` and keeps the original
> value, so the *first* requester of a model decides how it loads for everyone
> who follows, until it is evicted. See [Auth and Limits](auth-and-limits.md).

## Autoscaling: the second and third replica

Each `Processor` owns one long-lived `autoscaling_loop` created in its
constructor (`processor.py:229`). Every `NDIF_AUTOSCALING_INTERVAL_S` seconds,
*only while `READY`*, it peeks the queue head and compares `enqueued_at`:

- If the head has waited longer than `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` **and**
  the pool is smaller than `NDIF_AUTOSCALING_MAX_REPLICAS`, it deploys one more
  replica and then sleeps `NDIF_AUTOSCALING_BACKOFF_S` before re-checking.
- Otherwise it sleeps one interval.

The signal is *head wait time*, not queue depth: a hundred fast requests that
never leave anything at the head for 30 seconds will not trigger a scale-up,
while one long-blocked request will. The backoff exists because `scale_up`
returns as soon as the new replica is *ready*, not once it has drained anything
— without it, a slow-draining queue would keep firing scale-ups until it hit the
ceiling.

Scale-*down* does not exist here. Replicas leave the pool only by eviction,
cancellation, or a purge; the controller is what reclaims their GPU memory
(see [Deployments and Eviction](deployments-and-eviction.md)).

| Setting | Default | Effect |
|---|---|---|
| `NDIF_QUEUE_KEY` | `queue` | Redis list the API pushes to and the dispatcher pops from |
| `NDIF_QUEUE_FETCH_TIMEOUT_S` | `10` | Blocking-pop timeout; also the idle-loop tick |
| `NDIF_QUEUE_FETCH_BATCH_MAX` | `32` | Max requests drained per iteration |
| `NDIF_AUTOSCALING_INTERVAL_S` | `5` | How often a Processor checks its queue head |
| `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` | `30` | Head wait that triggers a scale-up |
| `NDIF_AUTOSCALING_BACKOFF_S` | `120` | Pause after a scale-up |
| `NDIF_AUTOSCALING_MAX_REPLICAS` | `3` | Replica ceiling per model, via autoscaling |

All are read once at import into a frozen `QueueConfig`
(`src/ndif/services/api/queue/config.py:72`) — changing one means restarting the
API.

## Requeue, cancel, purge

Three things can pull a request back out of flight:

- **Eviction.** If a dispatch fails with a lookup error, a dead actor, or
  `CachedActorError` (the actor's weights were moved to CPU), the replica drops
  itself and hands the in-flight request back to the *front* of the queue,
  keeping its original `enqueued_at` so the autoscaler still sees its true wait
  (`src/ndif/services/api/queue/replica.py:231`). The user sees no error.
- **Operator cancel.** `ndif kill <request_id>` goes through the dispatcher's
  event stream: if the request is queued it is removed and errored; if it is
  executing, the replica's worker is cancelled and the request errored.
- **Purge.** A Ray connection error makes the dispatcher error every queued
  request for every model, clear the queues, cancel all replicas, and reconnect
  (`dispatcher.py:151`).

## What "fair" means here

Not much, and it is worth being explicit about it:

- **Across models:** a single global FIFO on the Redis list, then independent
  per-model lines. A burst for one model does not delay another model's requests
  beyond the shared drain loop.
- **Within a model:** strict FIFO, except that a key tagged `priority` is
  prepended (`src/ndif/services/api/auth.py:48`). One user submitting a hundred
  requests occupies the line ahead of a user who submits one.
- **Per user:** nothing. There is no per-key concurrency cap, no quota, and no
  rate limit anywhere on the request path.
- **Head-of-line blocking is real.** A replica handles one request at a time and
  a long-running block holds it for up to
  `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` (default 3600). The only relief is
  autoscaling up to the ceiling.

## Related

- [Queue internals](../developing/queue-internals.md) — the dispatcher /
  processor / replica loops line by line, including the worker task lifecycle.
- [Deployments and Eviction](deployments-and-eviction.md) — what happens on the
  other side of `controller.deploy`, and why a replica can vanish mid-queue.
- [Debug a stuck request](../runbooks/debug-a-stuck-request.md) — using
  `ndif queue` to see the live per-model queues and replica state.
