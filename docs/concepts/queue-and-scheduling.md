---
title: Queue and Scheduling
one_liner: One Redis list feeding per-model in-memory priority queues inside a single dispatcher process — how a request finds its model, when a second replica appears, and what "fair" does and doesn't mean here.
tags: [concepts, queue, redis, api]
related: [docs/concepts/request-lifecycle.md, docs/concepts/deployments-and-eviction.md, docs/concepts/status-and-results.md, docs/developing/queue-internals.md, docs/reference/env-vars.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/services/api/queue/config.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/queue/request_queue.py, src/ndif/services/api/app.py, src/ndif/services/api/gunicorn_conf.py]
---

# Queue and Scheduling

## What this covers

The queueing model: where requests actually wait, how one is routed to a model,
what causes a new replica to appear, and what the scheduler does not do. The
loops themselves are walked through in
[Queue internals](../developing/queue-internals.md).

Two facts frame it:

1. **There are two queues, not one.** A single Redis list is the boundary
   between the API's *N* gunicorn workers and the *one* dispatcher process. Past
   that boundary, every request lives in an in-memory `RequestQueue` (an
   `asyncio.PriorityQueue`) owned by a `Processor` — one per `model_key`. The
   Redis list is the multi-producer / single-consumer handoff; the per-model
   queues are where waiting actually happens.
2. **Scheduling is FIFO with one escape hatch.** There is no weighting, no
   round-robin between users, no aging, and no fairness across models beyond
   "each model drains its own line". A `priority` API key sorts ahead of normal
   traffic; everything else is arrival order.

## The shape

```
api worker 1 ─┐
api worker 2 ─┼─ LPUSH "queue" ─→ Redis list ─→ BRPOP ─→ Dispatcher
api worker N ─┘                                            │
                                          routes by model_key
                                            ┌──────────────┴──────────────┐
                                     Processor(gpt2)            Processor(llama-70b)
                                     RequestQueue               RequestQueue
                                     Replica ×1..3              Replica ×1..3
                                        │                          │
                                   run.remote()               run.remote()
                                   ModelActor                 ModelActor
```

The dispatcher is not a container — it is a process the API's gunicorn master
spawns at startup (`src/ndif/services/api/gunicorn_conf.py` `on_starting`). It
is *spawned*, not forked, so the telemetry providers' background threads are
created fresh in it.

## Enqueue and drain

The API pickles the whole `BackendRequestModel` — payload included — and
`LPUSH`es it onto the list named by `NDIF_QUEUE_KEY`. The dispatcher `BRPOP`s
the other end, which makes it FIFO, then drains up to
`NDIF_QUEUE_FETCH_BATCH_MAX - 1` more with non-blocking `RPOP` to amortize
round-trips (`src/ndif/services/api/queue/dispatcher.py` `Dispatcher.get`). The
blocking pop times out every `NDIF_QUEUE_FETCH_TIMEOUT_S` so an idle loop still
wakes to drain the error queue and notice a broken Ray connection.

> **Gotcha:** once a request is popped it lives only in the dispatcher's memory.
> Restart the API container and every queued-but-not-yet-running request is gone
> — the client keeps waiting on a websocket that will never see another update.
> Requests still sitting in the Redis list survive.

## How a request finds its model

The target is `request.model_key`, chosen entirely by the client: nnsight's
`to_model_key()` produces `"<import.path.ClassName>:<model id>"` (e.g.
`nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", "revision": null, "task": "text-generation"}`).
The API never validates it — `Dispatcher.dispatch` uses it as a dict key and
creates a `Processor` for any key it has not seen.

So a typo'd or unavailable model key creates a real `Processor`, which asks the
controller to deploy it, which fails in the evaluator — and the user gets an
`ERROR` after a provisioning attempt, not a fast rejection. There is no
allow-list of deployable models on the request path.

A `Processor` is never torn down. An idle one sits with an empty pool and is
reused when the next request for that model arrives.

## Provisioning: the first replica

`enqueue` calls `ensure_started`, which no-ops if a replica already exists or
setup is already underway (`src/ndif/services/api/queue/processor.py`
`Processor.ensure_started`). Otherwise `start()` asks the controller for the
model's current replicas; it adopts whatever is listed, or asks for one new one
via `Replica.provision`. The client sees `PROVISIONING`, then `DEPLOYING`, then
`QUEUED` with a position once the pool is serving.

Each `Replica` runs a worker task that pulls from the *shared* per-model queue,
so replicas of the same model compete for the same line — the first free replica
takes the next request in service order. Nothing partitions requests across
replicas in advance.

> **Gotcha:** the `Processor` records `trust_remote_code` (granted by the
> `trusted` or the `trust_remote_code` tag) from the request that first
> provisions it and passes it into the `DeploymentConfig` it sends the
> controller as its `trusted` — which is what allows a model key asking for
> `trust_remote_code` to be deployed at all. A re-provision keeps the original value, so the
> *first* requester of a model decides who it was deployed as for everyone who
> follows, until it is evicted. See
> [Auth and Limits](auth-and-limits.md#the-trusted-flag).

## Autoscaling: the second and third replica

Each `Processor` owns one long-lived `Processor.autoscaling_loop`. Every
`NDIF_AUTOSCALING_INTERVAL_S` seconds, *only while serving*, it takes
`RequestQueue.oldest()` — the longest-waiting request across *both* priority
groups (`src/ndif/services/api/queue/request_queue.py`) — and:

- if it has waited longer than `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` **and** the
  pool is smaller than `NDIF_AUTOSCALING_MAX_REPLICAS`, deploys one more replica,
  then sleeps `NDIF_AUTOSCALING_BACKOFF_S` before re-checking;
- otherwise sleeps one interval.

The signal is *longest wait*, not queue depth: a hundred fast requests that never
leave anything waiting 30 seconds will not trigger a scale-up, while one
long-blocked request will. It is the oldest request rather than the next one to
be served, because the next one is the oldest *priority* request, and a starved
normal one can be far older.

The backoff exists because `scale_up` returns as soon as the new replica is
*ready*, not once it has drained anything — without it, a slow-draining queue
would keep firing scale-ups until it hit the ceiling.

Scale-*down* does not exist here. Replicas leave the pool only by eviction,
cancellation, or a purge; the controller is what reclaims their GPU memory
(see [Deployments and Eviction](deployments-and-eviction.md)).

All queue and autoscaling settings, with defaults, are in
[Environment variables](../reference/env-vars.md#queue-and-autoscaling). They
are read once at import into a frozen `QueueConfig`, so changing one means
restarting the API.

## Requeue, cancel, purge

Three things can pull a request back out of flight:

- **Eviction.** If a dispatch fails with a lookup error, a dead actor, or
  `CachedActorError` (the actor's weights were moved to CPU), the replica drops
  itself and re-enqueues the in-flight request with `prepend=True`
  (`src/ndif/services/api/queue/replica.py` `Replica.dispatch`), which puts it
  at the front of *its own* priority group, not the whole queue. It keeps its
  original `enqueued_at`, so the autoscaler still sees its true wait. The user
  sees no error.
- **Operator cancel.** `ndif kill <request_id>` goes through the dispatcher's
  event stream. A queued request is removed and errored. For an executing one,
  the replica's worker task is cancelled and the request errored to the user;
  the Ray call the worker was awaiting is not cancelled, so the actor runs the
  block to completion and its final response can still land
  ([Killed jobs](../runbooks/trace-a-users-failed-job.md#killed-jobs)).
- **Purge.** A Ray connection error makes the dispatcher error every queued
  request for every model, clear the queues, cancel all replicas, and reconnect
  (`Dispatcher.purge`).

## What "fair" means here

Not much, and it is worth being explicit about it:

- **Across models:** a single global FIFO on the Redis list, then independent
  per-model lines. A burst for one model does not delay another model's requests
  beyond the shared drain loop.
- **Within a model:** two FIFO groups — every `priority`-tagged request is
  served before every normal one, and each group is FIFO by arrival. One user
  submitting a hundred requests occupies the line ahead of a user who submits
  one, within their group. There is no aging, so sustained priority traffic
  starves normal traffic indefinitely; the autoscaler is the only relief, and it
  caps at `NDIF_AUTOSCALING_MAX_REPLICAS`.
- **Per user:** nothing. There is no per-key concurrency cap, no quota, and no
  rate limit anywhere on the request path.
- **Head-of-line blocking is real.** A replica handles one request at a time,
  and `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` is **unset by default** — so a
  block holds its replica until it finishes. The only relief is autoscaling up to
  the ceiling. Set the timeout on a shared deployment.

## Related

- [Queue internals](../developing/queue-internals.md) — the dispatcher /
  processor / replica loops, including the worker task lifecycle.
- [Deployments and Eviction](deployments-and-eviction.md) — what happens on the
  other side of `controller.scale`, and why a replica can vanish mid-queue.
- [Debug a stuck request](../runbooks/debug-a-stuck-request.md) — using
  `ndif queue` to see the live per-model queues and replica state.
