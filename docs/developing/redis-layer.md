---
title: The Redis Layer
one_liner: common/redis/* — the coalesced status/env caches, the CLI event stream, and response pub/sub, with the exact keys, TTLs and handshakes each one uses.
tags: [internals, dev, redis, api, queue, cli]
related: [docs/reference/redis-keys.md, docs/developing/providers.md, docs/developing/queue-internals.md, docs/developing/api-service.md, docs/concepts/status-and-results.md, docs/concepts/queue-and-scheduling.md, docs/reference/env-vars.md, docs/developing/cli-internals.md]
sources: [src/ndif/common/redis/__init__.py, src/ndif/common/redis/status.py, src/ndif/common/redis/env.py, src/ndif/common/redis/events.py, src/ndif/services/api/app.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/common/schema/request.py, src/ndif/cli/lib/events.py]
---

# The Redis Layer

## What this covers

`src/ndif/common/redis/` holds the key, stream and channel *names* that separate
NDIF processes coordinate through, plus the TTL/timeout knobs that go with them.
It is the keys package only — the connection lives in
`ndif.common.providers.redis` (see [Providers](providers.md)). This page is the
home of the coalesced `/status` / `/env` cache, the CLI event stream, response
pub/sub, and the `ray:connected` flag.

| Mechanism | Shape | Between |
|---|---|---|
| Request queue | List (`lpush` / `brpop`) | API workers → dispatcher |
| Coalesced caches (`/status`, `/env`) | String + lock + trigger stream + pub/sub | API workers ⇄ dispatcher |
| Operational events | Stream + per-caller reply list | CLI → dispatcher → CLI |
| Response publishing | Pub/sub channel per session | Anything that advances a request → the client's websocket |

The queue itself is configured in `src/ndif/services/api/queue/config.py` and
covered in [Queue Internals](queue-internals.md). For the key-by-key table see
[Redis Keys](../reference/redis-keys.md).

## The constraint that shapes it

**Only the dispatcher process holds a Ray client.** API workers never call
`ray.init()`; the dispatcher is a separate process spawned by gunicorn's
`on_starting` and is the sole owner of the connection
(`src/ndif/services/api/queue/dispatcher.py` `Dispatcher.connect`). So an
endpoint like `/status`, whose data lives on the controller actor, cannot fetch
it directly. A per-worker RPC would also multiply a heavy controller call by
worker count and request rate, and Redis is already a hard dependency, so the
coordination runs over it.

## The coalesced cache

`src/ndif/common/redis/status.py` and `src/ndif/common/redis/env.py` are the
same four-key pattern with different names and TTLs:

| Role | Status | Env | Type |
|---|---|---|---|
| Cached JSON | `status` | `env` | String, TTL'd |
| Coalescing lock | `status:requested` | `env:requested` | String, `SET NX EX` |
| Refresh trigger | `status:trigger` | `env:trigger` | Stream, `maxlen≈16` |
| Wake-up | `status:ready` | `env:ready` | Pub/sub channel |

| Knob | Env var | Default | What it bounds |
|---|---|---|---|
| `STATUS_TTL_S` | `NDIF_STATUS_TTL_S` | `60` | How long a cached status is served |
| `STATUS_TIMEOUT_S` | `NDIF_STATUS_TIMEOUT_S` | `60` | How long `/status` waits, how long the worker bounds the controller call, and the lock's TTL |
| `ENV_TTL_S` | `NDIF_ENV_TTL_S` | `300` | How long a cached env is served — env changes only on redeploy |
| `ENV_TIMEOUT_S` | `NDIF_ENV_TIMEOUT_S` | `60` | Same three roles as `STATUS_TIMEOUT_S` |

The lock is bounded by the *timeout*, not the TTL: raising `NDIF_STATUS_TTL_S`
doesn't lengthen it.

### The read path

Both endpoints call `_coalesced_fetch` (`src/ndif/services/api/app.py`):

1. `GET cache_key`. Hit → return the bytes as `application/json`, done.
2. Miss → subscribe to `ready_channel` **first**, then `GET` again. The re-check
   closes the race where a refresh landed between the first GET and the
   subscribe.
3. `SET requested_key "1" NX EX timeout_s`. Exactly one concurrent caller wins;
   the winner appends one entry to `trigger_stream` with
   `maxlen=16, approximate=True`. Losers skip straight to waiting.
4. Wait up to `timeout_s` on the pub/sub listen loop. Any other message means
   re-`GET` the cache and return it; `"error"` raises **503**; the
   `asyncio.timeout` firing raises **504**.

### The refresh path

`Dispatcher.status_worker` and `Dispatcher.env_worker`
(`src/ndif/services/api/queue/dispatcher.py`) are structurally identical tasks
started by `Dispatcher.dispatch_worker`. Each blocks on `xread` with
`block=0, count=1`, so one refresh happens per trigger, not per waiting request:

```python
messages = await client.xread({STATUS_TRIGGER_STREAM: last_id}, block=0, count=1)
...
handle = controller_handle()
status = await asyncio.wait_for(handle.status.remote(), timeout=STATUS_TIMEOUT_S)

await client.set(STATUS_KEY, json.dumps(status, default=str), ex=STATUS_TTL_S)
await client.delete(STATUS_REQUESTED_KEY)
await client.publish(STATUS_READY_CHANNEL, "ok")
```

On failure the worker logs, pushes the exception onto the dispatcher's
`error_queue` — which makes the dispatcher recheck the Ray connection and
possibly reconnect — **deletes the lock** so the next request can retry
immediately, publishes `"error"` so waiters get a 503 instead of hanging, and
yields with `asyncio.sleep(0)` so a persistently failing controller can't peg
the event loop. A second API worker that misses during a refresh loses the
`SET NX`, skips the `XADD`, and is woken by the same `PUBLISH`.

### Why a cache with a trigger stream instead of RPC

- **Fan-in.** Any number of API workers and any request rate collapse to at most
  one controller call per coalescing window; the controller's `status()` walks
  every node and deployment.
- **Direction.** The API only ever *writes* a trigger and *waits*; it never holds
  a Ray handle, so the API image doesn't need the `ray` extra and an API worker
  can't wedge on a slow control-plane call beyond its own timeout.
- **Crash safety without a coordinator.** The lock's `EX timeout_s` is a
  dead-man's switch: if the winner or the worker dies mid-refresh, the lock
  expires and the next request triggers a fresh attempt.
- **The stream is the queue, the channel is the doorbell.** A stream entry
  survives until read, so a trigger isn't lost if the worker is busy; pub/sub is
  fire-and-forget, so a wake-up to nobody costs nothing.

## The events stream

`src/ndif/common/redis/events.py` is a different shape: these events act on the
dispatcher's **own in-memory state** (its `Processor` objects), not on the
controller. The CLI produces them; `Dispatcher.events_worker` consumes them.

Everything rides `dispatcher:events` (`EVENTS_STREAM`), with the entry's
`event_type` field selecting a handler:

| `event_type` | Constant | Extra fields | Semantics |
|---|---|---|---|
| `queue_state_request` | `EVENT_QUEUE_STATE` | `response_key` | Request/response — snapshot of every Processor |
| `kill_request` | `EVENT_KILL` | `response_key`, `request_id` | Request/response — a queued request is removed and errored; an executing one is errored to the client and cancelled on the model actor, with the dispatch cancelled only if the actor does not finish it within a few seconds |
| `reconcile_model` | `EVENT_RECONCILE` | `model_key` | Fire-and-forget — re-sync one Processor's replica pool |

The request/response ones do their own tiny RPC: the caller mints a unique
`response_key`, appends the event, and blocks on `brpop`
(`src/ndif/cli/lib/events.py` `_request`):

```python
response_key = f"ndif:cli:{event_type}:{os.getpid()}:{time.time_ns()}"
client.xadd(EVENTS_STREAM, {"event_type": event_type,
                            "response_key": response_key, **fields},
            maxlen=64, approximate=True)
result = client.brpop(response_key, timeout=timeout)
```

The dispatcher replies with `Dispatcher._respond`: one `lpush` of the JSON
payload, then `expire(response_key, EVENT_RESPONSE_TTL_S)` — 30 seconds — so a
reply nobody collected is reaped. The CLI's default timeout is 5s.
`notify_reconcile` is best-effort and swallows every error: a Redis hiccup must
not fail a deploy that already succeeded on the controller. Handlers are
individually try/excepted so one malformed event can't kill the worker.

## Response publishing

A request carries a `session_id` when the client opened a `/subscribe` websocket.
That id **is** the pub/sub channel name — there is no prefix.
`BackendRequestModel.respond` / `arespond` (`src/ndif/common/schema/request.py`)
publish to it:

```python
if self.session_id:
    RedisProvider.sync_client.publish(
        self.session_id,
        response.pickle() if pickled else response.model_dump_json(),
    )
elif status != Status.LOG:
    ObjectStoreProvider.put(_response_key(self.id), ...)   # responses/{id}.json
```

A status update is JSON. A COMPLETED response that carries the result inline is
published as `torch.save` bytes (`pickled=True`), and the websocket handler
forwards it as a binary frame — `subscribe` tells the two apart by whether the
message starts with `{`. Non-blocking requests have no `session_id`, so the
latest response is written to the object store as JSON instead and polled via
`GET /response/{id}`; `LOG` updates are dropped on that path.

Publishers include the queue's processor and replica workers, the dispatcher
(when an operator kills a request), the model actor (`BaseModelDeployment.run`
in `src/ndif/services/ray/deployments/modeling/base.py`), and the sandbox's
stdout forwarder — the `on_log` callback `SandboxHost.run_in_runner` hands the
driver, fired for each `PRINT` event in `SandboxDriver.next_event`
(`src/ndif/services/ray/sandbox/driver.py`). The API itself does *not* publish
`RECEIVED` — it returns that response over HTTP.

A channel is not one message per request. Every line the user's code prints
becomes a separate `Status.LOG` publish — `LogStream.write`
(`src/ndif/services/ray/deployments/modeling/util.py`) emits one per complete
line — so a chatty block produces hundreds of publishes on one channel.

> **Gotcha:** Redis pub/sub has no persistence or replay. A message published
> while nobody is subscribed is gone. That is why `/subscribe` subscribes to the
> channel *before* it sends the client its `session_id`.

> **Gotcha:** a subscriber whose output buffer exceeds the server's
> `client-output-buffer-limit pubsub` is *disconnected*, and everything buffered
> for it is lost — on this channel that means a COMPLETED response, result
> included. The model actor therefore caps what may ride a single publish at
> `RedisProvider.max_publish_bytes`, derived from those same server limits
> (`min(soft, hard // 2)`, 8 MiB against stock redis:7); larger results go to the
> object store. The cap bounds one message, not a queue of them: a subscriber
> that stops draining while several near-cap results are published can still be
> dropped.

## The `ray:connected` flag

A plain string key, no TTL, owned entirely by `Dispatcher.connect`. It waits for
Redis, deletes the flag, deletes `status`, `status:requested`, `env` and
`env:requested` (those describe a cluster the dispatcher is no longer attached
to, and clearing the locks means the first request after a reconnect triggers a
fresh refresh), loops until Ray is reachable *and* the Controller actor resolves,
then sets the flag to `"1"`. The API uses it as a route dependency,
`require_ray_connection`, to 503 early rather than enqueue work the backend
can't serve — it guards `/request`, `/status`, `/env` and `/connected`.

Because nothing expires it, the flag outlives a dispatcher that dies outright:
`require_ray_connection` keeps passing, `GET /connected` still reports
"connected", and requests accumulate in the Redis list unserved.

## Gotchas

> **The workers start at `$`.** Both cache workers and the events worker begin
> with `last_id = "$"`, meaning "only entries added from now on". Triggers or CLI
> events appended while the dispatcher was down are never seen. A waiting
> `/status` then 504s after `STATUS_TIMEOUT_S`, and the lock expires at the same
> moment, so the *next* request retries cleanly. A `ndif queue` issued against a
> dead dispatcher raises `TimeoutError` with "Is the API running?".

> **Exactly one dispatcher is assumed.** `xread` is used, not consumer groups, so
> every dispatcher reading `status:trigger` would refresh, and every one reading
> `dispatcher:events` would handle the event and `lpush` a reply. Gunicorn starts
> exactly one.

> **Redis is not persisted in the dev stack.** The `redis` compose service mounts
> no volume. A restart drops queued requests, both caches, and every in-flight
> pub/sub subscription — connected clients see their jobs go silent.

> **Keys are unprefixed and global.** `status`, `env`, `queue`, `ray:connected`
> are top-level names in database 0. Pointing two NDIF deployments at one Redis
> makes them fight; give each its own instance or its own database number in
> `NDIF_REDIS_URL`.

## Related

- [Redis Keys](../reference/redis-keys.md) — every key, channel and stream in
  one table.
- [Providers](providers.md) — the three Redis clients and why one is binary.
- [Queue Internals](queue-internals.md) — the request list and what the
  dispatcher does with what it pops.
- [API Service](api-service.md) — the endpoints on the read side of these caches.
- [Status and Results](../concepts/status-and-results.md) — the `Status`
  lifecycle carried on the response channel.
