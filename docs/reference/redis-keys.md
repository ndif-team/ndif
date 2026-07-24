---
title: Redis Keys Reference
one_liner: Every Redis key, channel and stream NDIF uses — type, writer, reader, TTL and the env var that controls it.
tags: [reference, redis, api, queue, cli]
related: [docs/developing/redis-layer.md, docs/developing/providers.md, docs/developing/queue-internals.md, docs/reference/env-vars.md, docs/concepts/status-and-results.md, docs/runbooks/debug-a-stuck-request.md]
sources: [src/ndif/common/redis/status.py, src/ndif/common/redis/env.py, src/ndif/common/redis/events.py, src/ndif/services/api/queue/config.py, src/ndif/services/api/app.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/common/schema/request.py, src/ndif/cli/lib/events.py]
---

# Redis Keys Reference

## What this covers

Every key, pub/sub channel and stream any part of `src/ndif` touches, grouped by
subsystem. Names are literal unless shown with `{braces}`, which mark interpolated
parts. All names are top-level in database 0 — there is no global prefix.

The constants live in `src/ndif/common/redis/`, but not all of them: the queue key
comes from `src/ndif/services/api/queue/config.py`, the `ray:connected` flag is an
inline literal in the dispatcher, and the response channels are named by a
request's `session_id`. For *why* each mechanism is shaped the way it is, see
[The Redis layer](../developing/redis-layer.md).

## Request queue

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `queue` | List | API `POST /request` — `lpush` on the **binary** client (`app.py:180`) | Dispatcher `get()` — `brpop` (`dispatcher.py:121`) then up to `fetch_batch_max-1` `rpop`s (`:128`) | none | One pickled `BackendRequestModel` per element, payload blob included |

The name is `NDIF_QUEUE_KEY`, default `queue`
(`src/ndif/services/api/queue/config.py:58`). `lpush` + `brpop` gives FIFO. This is
the only key that holds non-UTF-8 data, which is why `RedisProvider` keeps a
separate `async_bytes_client` — decoding would corrupt the pickle.

The blocking pop uses `NDIF_QUEUE_FETCH_TIMEOUT_S` (default `10`) so an idle
dispatch loop still wakes to drain errors; `NDIF_QUEUE_FETCH_BATCH_MAX` (default
`32`) caps one drain.

## Control-plane health

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `ray:connected` | String | Dispatcher `connect()` — `delete` on entry (`dispatcher.py:85`), `set "1"` once Ray *and* the Controller actor answer (`:109`) | API `require_ray_connection` dependency (`app.py:114`) | none — presence is the signal | `"1"` |

Guards `POST /request`, `GET /status`, `GET /env` and `/connected`; absence yields
a 503 with "compute backend is reconnecting".

## Cluster status cache

Constants in `src/ndif/common/redis/status.py`.

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `status` | String | Dispatcher `status_worker` — `set ... ex=STATUS_TTL_S` (`dispatcher.py:241`); deleted on Ray reconnect (`:93`) | API `_coalesced_fetch` (`app.py:223`, `:232`, `:252`) | `NDIF_STATUS_TTL_S`, default **60s** | The controller's `status()` dict, JSON-encoded |
| `status:requested` | String | API `SET NX EX timeout_s` (`app.py:237`); deleted by the worker on both success and failure (`dispatcher.py:244`, `:249`) and on Ray reconnect (`:93`) | Nobody reads the value — only the `NX` result matters | `NDIF_STATUS_TIMEOUT_S`, default **60s** | `"1"`. Coalescing lock; the TTL is a dead-man's switch |
| `status:trigger` | Stream | API `xadd(..., maxlen=16, approximate=True)`, winner of the lock only (`app.py:238`) | Dispatcher `status_worker` — `xread(block=0, count=1)` from `$` (`dispatcher.py:228`) | capped at ~16 entries, no time TTL | `{"t": "1"}` — the entry is a doorbell, its content is unused |
| `status:ready` | Pub/sub channel | Dispatcher `status_worker` — `"ok"` (`dispatcher.py:245`) or `"error"` (`:250`) | Waiting `/status` requests (`app.py:228`) | n/a | `"ok"` / `"error"` |

## Cluster env cache

Constants in `src/ndif/common/redis/env.py`. Identical mechanism, different TTL —
the cluster env (python version + installed packages) changes only on a redeploy.

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `env` | String | Dispatcher `env_worker` (`dispatcher.py:280`); deleted on Ray reconnect (`:93`) | API `_coalesced_fetch` via `GET /env` (`app.py:288`) | `NDIF_ENV_TTL_S`, default **300s** | The controller's `env()` dict, JSON-encoded |
| `env:requested` | String | API `SET NX EX timeout_s` (`app.py:237`); deleted by the worker (`dispatcher.py:281`, `:286`) and on reconnect (`:93`) | Only the `NX` result | `NDIF_ENV_TIMEOUT_S`, default **60s** | `"1"` |
| `env:trigger` | Stream | API `xadd(..., maxlen=16)` (`app.py:238`) | Dispatcher `env_worker` `xread` (`dispatcher.py:267`) | ~16 entries | `{"t": "1"}` |
| `env:ready` | Pub/sub channel | Dispatcher `env_worker` — `"ok"` (`dispatcher.py:282`) / `"error"` (`:287`) | Waiting `/env` requests | n/a | `"ok"` / `"error"` |

## Operational events (CLI → dispatcher)

Constants in `src/ndif/common/redis/events.py`.

| Key | Type | Written by | Read by | TTL | Holds |
|---|---|---|---|---|---|
| `dispatcher:events` | Stream | CLI `xadd(..., maxlen=64, approximate=True)` — `ndif queue`, `ndif kill` (`cli/lib/events.py:38,40`), and `notify_reconcile` after `ndif deploy` / `ndif evict` (`:71,73`) | Dispatcher `events_worker` — `xread(block=0, count=1)` from `$` (`dispatcher.py:309`) | capped at ~64 entries, no time TTL | `event_type` plus per-type fields |
| `ndif:cli:{event_type}:{pid}:{time_ns}` | List | Dispatcher `_respond` — `lpush` of the JSON reply, then `expire` (`dispatcher.py:329`) | The CLI process that minted it — `brpop`, default timeout 5s (`cli/lib/events.py:40`) | `EVENT_RESPONSE_TTL_S` = **30s**, hardcoded (`events.py:27`) | One JSON reply |

Event types carried on `dispatcher:events`:

| `event_type` | Fields | Dispatcher does |
|---|---|---|
| `queue_state_request` | `response_key` | Replies with `{"processors": {model_key: snapshot}}` (`dispatcher.py:332`) |
| `kill_request` | `response_key`, `request_id` | Dequeues the request, or cancels it on its replica; replies `removed_from_queue` / `cancelled_execution` / `not_found` (`dispatcher.py:341`) |
| `reconcile_model` | `model_key` | Re-syncs that Processor's replica pool. Fire-and-forget, no reply (`dispatcher.py:359`) |

The response key is minted per call as
`f"ndif:cli:{event_type}:{os.getpid()}:{time.time_ns()}"`
(`cli/lib/events.py:37`), so it is unique per invocation and never reused.

## Response channels

| Channel | Type | Published by | Subscribed by | TTL | Carries |
|---|---|---|---|---|---|
| `{session_id}` | Pub/sub channel | `BackendRequestModel.respond` (sync, `request.py:149`) and `arespond` (async, `:173`) — so the queue's processor/replica workers, the dispatcher's kill handler (`dispatcher.py:346`), the model actor (`modeling/base.py:261` onward), `LogStream.write` (`modeling/util.py:35`), and `SandboxModelDeployment.next_event` (`sandbox/model.py:226`) | The API's `/subscribe` websocket handler (`app.py:386`), which forwards each message verbatim to the client | n/a | `BackendResponseModel.model_dump_json()` — `id`, `status`, `description`, optional `data` |

`session_id` is a bare `uuid4().hex` generated per websocket (`app.py:380`) and
used as the channel name with no prefix. The handler subscribes *before* sending
the id to the client, so the channel is live before any status can be published —
pub/sub has no replay.

**Traffic is not one message per request.** A channel carries every status
transition (`RECEIVED` → `QUEUED` → `RUNNING` → `COMPLETED`/`ERROR`) *plus* one
`Status.LOG` message per line the user's code prints. `LogStream.write`
(`modeling/util.py:31`) buffers stdout and publishes one LOG per complete line, and
in the sandboxed path each `PRINT` event the runner subprocess sends is republished
as a LOG (`sandbox/model.py:226`). A chatty traced block therefore produces
hundreds of publishes on one channel, all forwarded down the same websocket.

A request with **no** `session_id` (non-blocking submit) has no channel; its latest
response goes to the object store instead, and `LOG` updates are dropped there —
so a non-blocking job's prints are not retrievable.

## Not in Redis

Two things that look like they belong here but are object-store keys, not Redis:

| Key | Store | Written by | Read by |
|---|---|---|---|
| `responses/{request_id}.json` | Object store bucket (`NDIF_OBJECT_STORE_BUCKET`) | `respond`/`arespond` for non-blocking requests (`request.py:153`, `:179`) | `GET /response/{id}` (`app.py:314`) |
| `{request_id}.pt` | Same bucket | Model actor `upload_bytes` (`modeling/base.py:550`) | The client, via a presigned URL returned on the COMPLETED response (`modeling/base.py:561`) |

Ray's internal GCS also uses a Redis-like store, but that is Ray's own, not this
one, and none of the keys above appear in it.

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
`PING` at all (`src/ndif/cli/lib/checks.py:21`).

> **Gotcha:** deleting `status:requested` by hand while a refresh is genuinely in
> flight lets a second refresh start; deleting `ray:connected` makes every guarded
> endpoint 503 until the dispatcher next completes `connect()`. Neither is
> corrupting, but both will confuse a debugging session.

> **Gotcha:** these names are unprefixed, so two NDIF deployments pointed at one
> Redis will share `queue`, `status` and `ray:connected` and interfere. Give each
> its own instance, or its own database number in `NDIF_REDIS_URL`.

## Related

- `docs/developing/redis-layer.md` — the handshakes these keys implement.
- `docs/developing/providers.md` — the three Redis clients and their modes.
- `docs/developing/queue-internals.md` — what happens after a request is popped.
- `docs/reference/env-vars.md` — the TTL and timeout vars in the wider table.
