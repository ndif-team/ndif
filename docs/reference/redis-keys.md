---
title: Redis Keys Reference
one_liner: Every Redis key, channel and stream NDIF uses — type, writer, reader, TTL and the env var that controls it.
tags: [reference, redis, api, queue, cli]
related: [docs/developing/redis-layer.md, docs/developing/providers.md, docs/developing/queue-internals.md, docs/reference/env-vars.md, docs/concepts/status-and-results.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/common/redis/status.py, src/ndif/common/redis/env.py, src/ndif/common/redis/events.py, src/ndif/common/redis/__init__.py, src/ndif/services/api/queue/config.py, src/ndif/services/api/app.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/common/schema/request.py, src/ndif/cli/lib/events.py, src/ndif/services/ray/sandbox/driver.py]
---

# Redis Keys Reference

## What this covers

Every key, pub/sub channel and stream any part of `src/ndif` touches, grouped by
subsystem. Names are literal unless shown with `{braces}`, which mark interpolated
parts. All names are top-level in the database `NDIF_REDIS_URL` names — there is no prefix.

The constants live in `src/ndif/common/redis/`, but not all of them: the queue key
comes from `src/ndif/services/api/queue/config.py`, the `ray:connected` flag is an
inline literal in the dispatcher, and the response channels are named by a
request's `session_id`. For *why* each mechanism is shaped the way it is, see
[The Redis layer](../developing/redis-layer.md).

## Request queue

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `queue` | List | `POST /request` — `lpush` on the bytes client (`src/ndif/services/api/app.py` `create_request`) | `src/ndif/services/api/queue/dispatcher.py` `Dispatcher.get` — `brpop`, then up to `fetch_batch_max - 1` `rpop`s | none | One pickled `BackendRequestModel` per element, payload blob included |

The name is `NDIF_QUEUE_KEY`, default `queue` (`QueueConfig.queue_key`). `lpush` +
`brpop` gives FIFO. The pickles are not UTF-8, which is one reason
`RedisProvider` keeps a separate non-decoding `async_bytes_client`. The blocking
pop uses `NDIF_QUEUE_FETCH_TIMEOUT_S` so an idle dispatch loop still wakes to
drain errors; `NDIF_QUEUE_FETCH_BATCH_MAX` caps one drain.

## Control-plane health

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `ray:connected` | String | `Dispatcher.connect` — deleted on entry, set to `"1"` once Ray *and* the Controller actor answer | `src/ndif/services/api/app.py` `require_ray_connection` | none — presence is the signal | `"1"` |

Guards `POST /request`, `GET /status`, `GET /env` and `/connected`; absence yields
a 503 "compute backend is reconnecting".

## Cluster status and env caches

`/status` and `/env` share one mechanism (`src/ndif/services/api/app.py`
`_coalesced_fetch` on the API side; `Dispatcher.status_worker` and
`Dispatcher.env_worker` on the other). Constants in `src/ndif/common/redis/status.py`
and `src/ndif/common/redis/env.py`; rows below are for `status`, and the `env`
rows are identical with `env` in the name and `NDIF_ENV_TTL_S` (default **300s**)
/ `NDIF_ENV_TIMEOUT_S` (default 60s) as the TTLs.

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `status` | String | The worker — `set ... ex=STATUS_TTL_S`; deleted by `Dispatcher.connect` on (re)connect | `_coalesced_fetch` | `NDIF_STATUS_TTL_S`, default **60s** | The controller's `status()` dict, JSON |
| `status:requested` | String | `_coalesced_fetch` — `SET NX EX timeout_s`; deleted by the worker on success and failure, and on reconnect | Nobody reads the value — only the `NX` result matters | `NDIF_STATUS_TIMEOUT_S`, default **60s** | `"1"`. Coalescing lock; the TTL is a dead-man's switch |
| `status:trigger` | Stream | `_coalesced_fetch` — `xadd(..., maxlen=16, approximate=True)`, lock winner only | The worker — `xread(block=0, count=1)` from `$` | ~16 entries, no time TTL | `{"t": "1"}` — a doorbell; content unused |
| `status:ready` | Pub/sub channel | The worker — `"ok"` or `"error"` | Waiting `/status` requests | n/a | `"ok"` / `"error"` |

## Operational events (CLI → dispatcher)

Constants in `src/ndif/common/redis/events.py`.

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `dispatcher:events` | Stream | The CLI — `xadd(..., maxlen=64, approximate=True)`: `ndif queue` and `ndif kill` (`src/ndif/cli/lib/events.py` `_request`), and `notify_reconcile` after `ndif deploy` / `ndif evict` | `Dispatcher.events_worker` — `xread(block=0, count=1)` from `$` | ~64 entries, no time TTL | `event_type` plus per-type fields |
| `ndif:cli:{event_type}:{pid}:{time_ns}` | List | `Dispatcher._respond` — `lpush` of the JSON reply, then `expire` | The CLI process that minted it (`_request`) — `brpop`, default timeout 5 s | `EVENT_RESPONSE_TTL_S` = **30s**, hardcoded | One JSON reply |

The response key is minted per call from the PID and `time.time_ns()`, so it is
never reused. Event types on `dispatcher:events`:

| `event_type` | Fields | Dispatcher does |
|---|---|---|
| `queue_state_request` | `response_key` | Replies `{"processors": {model_key: snapshot}}` (`Dispatcher._handle_queue_state`) |
| `kill_request` | `response_key`, `request_id` | `Dispatcher._kill`: a queued request is dequeued and errored; an executing one has its dispatch-side replica worker cancelled, which errors the request; the model actor's execution is not interrupted. Replies `removed_from_queue` / `cancelled_execution` / `not_found` |
| `reconcile_model` | `model_key` | Re-syncs that Processor's replica pool. Fire-and-forget, no reply (`Dispatcher._handle_reconcile`) |

## Response channels

| Channel | Type | Published by | Subscribed by | TTL | Carries |
|---|---|---|---|---|---|
| `{session_id}` | Pub/sub channel | `src/ndif/common/schema/request.py` `BackendRequestModel.respond` / `arespond` — called by the queue's processor and replica workers, `Dispatcher._kill`, the model actor (`BaseModelDeployment.run`), `LogStream.write` (`src/ndif/services/ray/deployments/modeling/util.py`), and in the sandboxed path the `on_log` callback `SandboxHost.run_in_runner` hands `SandboxDriver` | The API's `/subscribe` websocket (`src/ndif/services/api/app.py` `subscribe`), on the bytes client, forwarding each message verbatim | n/a | A JSON `BackendResponseModel` (text frame), or — for a COMPLETED response carrying the result itself, whose blob is at most `RedisProvider.max_publish_bytes` — the response's `torch.save` bytes (binary frame) |

The cap bounds the blob, not the frame: the published frame is the pickled response,
up to ~1.5x the compressed blob (`torch.save`'s byte encoding). A near-cap result
already exceeds the soft pubsub limit, and about three undrained near-cap results
reach the hard limit.

`session_id` is a bare `uuid4().hex` minted per websocket and used as the channel
name with no prefix. The handler subscribes *before* sending the id to the client,
so the channel is live before any status can be published — pub/sub has no replay.

**What is published:** `QUEUED`, `RUNNING`, `COMPLETED`/`ERROR` and the other
transitions — but not `RECEIVED`, which is only the HTTP response to
`POST /request` (`create_request` builds it with `response`, not `respond`) —
plus one `Status.LOG` message per line the user's code prints. A chatty block
therefore produces hundreds of publishes on one channel.

A request with **no** `session_id` (non-blocking submit) has no channel: its latest
non-LOG response goes to the object store as JSON instead (see below), so its prints
are not retrievable.

## Not in Redis

Two object-store keys that look like they belong here:

| Key | Store | Written by | Read by |
|---|---|---|---|
| `responses/{request_id}.json` | Object store bucket (`NDIF_OBJECT_STORE_BUCKET`) | `respond`/`arespond` for non-blocking requests (key from `_response_key` in `src/ndif/common/schema/request.py`) | `GET /response/{id}` (`get_response`) |
| `{request_id}.pt` | Same bucket | `BaseModelDeployment.upload_bytes`, when the result exceeds the inline cap or the request is non-blocking | The client, via the presigned URL in the COMPLETED response's `data` |

## Inspecting a live instance

```bash
# Everything currently set (small enough to eyeball — NDIF uses ~10 keys)
redis-cli -u "$NDIF_REDIS_URL" keys '*'

# Queue depth, and whether the dispatcher thinks Ray is up
redis-cli -u "$NDIF_REDIS_URL" llen queue
redis-cli -u "$NDIF_REDIS_URL" get ray:connected

# The cached status and its remaining TTL
redis-cli -u "$NDIF_REDIS_URL" get status
redis-cli -u "$NDIF_REDIS_URL" ttl status

# Is a refresh in flight / stuck?
redis-cli -u "$NDIF_REDIS_URL" get status:requested
redis-cli -u "$NDIF_REDIS_URL" xlen status:trigger

# Watch every status update for one session (get the id from the client)
redis-cli -u "$NDIF_REDIS_URL" subscribe <session_id>
```

`ndif queue` reports the same queue state through the events stream without
touching Redis by hand, and `ndif info` / `ndif doctor` check that Redis answers
`PING` at all (`src/ndif/cli/lib/checks.py` `check_redis`).

> **Gotcha:** deleting `status:requested` by hand while a refresh is genuinely in
> flight lets a second refresh start; deleting `ray:connected` makes every guarded
> endpoint 503 until the dispatcher next completes `connect()`. Neither is
> corrupting, but both will confuse a debugging session.

> **Gotcha:** these names are unprefixed, so two NDIF deployments pointed at one
> Redis will share `queue`, `status` and `ray:connected` and interfere. Give each
> its own instance, or its own database number in `NDIF_REDIS_URL`.

## Related

- [redis-layer.md](../developing/redis-layer.md) — the handshakes these keys implement.
- [providers.md](../developing/providers.md) — the three Redis clients and their modes.
- [queue-internals.md](../developing/queue-internals.md) — what happens after a request is popped.
- [env-vars.md](env-vars.md) — the TTL and timeout vars in the wider table.
