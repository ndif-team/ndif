---
title: API Service
one_liner: The FastAPI ingress — how it boots under gunicorn, what the dependency chain does to a request, and how a request becomes a pickled queue entry and comes back as a result or a presigned URL.
tags: [internals, dev, api]
related: [docs/reference/http-api.md, docs/developing/queue-internals.md, docs/concepts/request-lifecycle.md, docs/concepts/auth-and-limits.md, docs/concepts/sandbox-execution.md, docs/developing/redis-layer.md, docs/reference/schemas.md, docs/reference/redis-keys.md]
sources: [src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/api/versioning.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/api/start.sh, src/ndif/common/schema/request.py, src/ndif/common/providers/redis.py, src/ndif/common/providers/objectstore.py]
---

# API Service

## What this covers

`src/ndif/services/api/` — the only part of NDIF a client talks to: one FastAPI
module (`app.py`), an auth layer, a version gate, and a gunicorn config. How it
boots, what runs per worker vs. per process, how a POST becomes a queue entry,
and how the result gets back out.

Three constraints shape the whole design:

1. **The API process cannot talk to Ray.** Only the dispatcher holds a Ray
   client connection. Everything the API needs from the cluster (status, env,
   dispatch) goes through Redis, which is why `/status` and `/env` are
   cache-and-wake-up dances rather than RPCs.
2. **The API forwards results but never produces them.** The model actor
   publishes a small result on the COMPLETED response and the `/subscribe`
   forwarder passes it to the client; a large one, or any non-blocking result,
   goes to the object store as a presigned URL. The cap is
   [Status and Results](../concepts/status-and-results.md)'s topic.
3. **Ingress is where trust is decided.** The `trusted` flag stamped here
   determines whether a caller's Python runs inside the model actor process or in
   a separate runner, and `trust_remote_code` beside it whether the caller may
   deploy a model whose key carries `trust_remote_code` — see
   [The `trusted` flag](#the-trusted-flag--read-this-before-you-deploy).

## Boot

`src/ndif/services/api/start.sh` is the entrypoint (registered as the `api`
service in `src/ndif/cli/service.py` `SERVICES`, so `ndif start api` runs it).
It exports `NDIF_SERVICE=api` for log labelling and `exec gunicorn --config
python:ndif.services.api.gunicorn_conf ndif.services.api.app:app`.

`src/ndif/services/api/gunicorn_conf.py` sets `bind` from `NDIF_API_PORT`
(8001), `workers` from `NDIF_API_WORKERS` (1), `timeout` from
`NDIF_API_TIMEOUT` (120), and `worker_class = "uvicorn.workers.UvicornWorker"`.
Two hooks do the interesting work:

- **`on_starting`** starts the queue dispatcher as a **spawned** (not forked)
  process, exactly once, in the gunicorn master. Its target `_run_dispatcher`
  imports the Influx and Loki providers and then `Dispatcher.start()`.
- **`post_fork`** imports `ndif.common.providers.influx` and
  `ndif.common.providers.loki` *in each worker*.

Both exist because the telemetry providers must connect after the fork; the
reasoning, and why `preload_app = True` breaks it, is in
[Telemetry Internals](telemetry-internals.md).

> **Gotcha:** the dispatcher lives in the *master's* child, not in a worker. Set
> `NDIF_API_WORKERS=4` and you get four web workers and still exactly one
> dispatcher — which is what you want, since the dispatcher holds all per-model
> queue state in process memory. See [Queue Internals](queue-internals.md).

## App-level middleware and handlers

`src/ndif/services/api/app.py` registers exactly three things at the app level:
`CORSMiddleware` (`allow_origins=["*"]`, `allow_credentials=False` — auth is the
`ndif-api-key` header, not a cookie, and browsers reject a wildcard origin with
credentials), and two exception handlers, `log_http_exception` (a structured
`event()`, ERROR at 5xx and INFO at 4xx, then `{"detail": ...}`) and
`log_unhandled_exception` (an ERROR event with traceback, then a generic 500).
The handlers are the only place ingress failures become observable: a rejected
key or an unreachable backend raises before any endpoint body runs.

## The dependency chain

**`require_ray_connection`** (`src/ndif/services/api/app.py`) — an app-level
route dependency, not a middleware. It reads the `ray:connected` Redis flag
(written by `src/ndif/services/api/queue/dispatcher.py` `Dispatcher.connect`) and
raises 503 if absent. Applied to `/request`, `/status`, `/env`, and `/connected`.

**`validate_request`** (`src/ndif/services/api/auth.py`) — the `/request` body
dependency. It parses the multipart `data` form field into a
`BackendRequestModel` (422 on a bad envelope), copies the `ndif-api-key` header
onto `request.api_key`, calls `verify_api_key`, and stamps `email` / `trusted` /
`trust_remote_code` / `priority` from the resolved `Identity` so they travel with
the request through the queue.

**`verify_api_key`** (`src/ndif/services/api/auth.py`) is the policy layer over
`PostgresProvider`. Auth is entirely optional: if `NDIF_POSTGRES_URL` is unset,
`PostgresProvider.enabled()` is False and the function returns `None`.

With Postgres configured, a key is valid **iff its row exists in `keys`** — key
issuance is a separate login/account service's job. `_KEY_QUERY` LEFT JOINs
`key_user_tag_assignments` / `user_tags`, so a key with no tags still returns one
row (tag NULL), distinguishing "known key, no tags" from "unknown key".

| Failure | Status |
|---|---|
| No key sent | 401 |
| Key present but not a UUID | 400 |
| Well-formed key not in `keys` | 403 |
| Postgres unreachable / errored | 503 (fail closed) |

### The `trusted` flag — read this before you deploy

`validate_request` stamps two things onto the request from the resolved
`Identity`: `email` and `tags` (the key's `user_tags`).

```python
identity = await verify_api_key(request.api_key)
if identity is not None:
    request.email = identity.email
    request.tags = set(identity.user_tags)    # overwrites any client value
elif "tags" not in request.model_fields_set:
    # Auth is off (NDIF_POSTGRES_URL unset), a trusted-network / dev mode.
    request.tags = {"trusted"}
```

`BackendRequestModel.tags` (`src/ndif/common/schema/request.py`) defaults to
empty, and the properties `trusted`, `trust_remote_code` and `priority` read it:
`trusted` is `"trusted" in tags` and is what keeps user code out of the model
process; `trust_remote_code` is that tag or `trusted`; `priority` is its tag.
With auth **on** the key's tags decide and any client-supplied value is
overwritten. With auth **off** the client's own `tags` are honored and an
unspecified value becomes `{"trusted"}`, so a dev can opt into the sandbox path
by sending `tags: []` without standing up Postgres.
[Auth and Limits](../concepts/auth-and-limits.md#trust_remote_code) defines the
`trust_remote_code` permission and the gate.

**What `trusted` decides on the GPU side:**

- A **trusted** request's traced block is executed **in-process inside the model
  actor, next to the loaded weights** — `SandboxModelDeployment.execute` defers
  to the base implementation when `request.trusted`
  (`src/ndif/services/ray/sandbox/model.py`).
- An **untrusted** request's block is shipped to a separate runner subprocess and
  driven over a Unix socket, so arbitrary user Python never runs in the actor
  process. This is process separation, not a security boundary — see
  [Sandbox Execution](../concepts/sandbox-execution.md).
- Whether the model loads with `trust_remote_code` is **not** `trusted`'s call;
  that is a field of the model key.

> **Gotcha:** "no Postgres configured" does not only mean "no auth". It means
> **a caller's arbitrary Python runs in-process next to the weights by default, and
> any caller may deploy a model whose key carries `trust_remote_code`** — a client
> can drop to the sandbox path with `tags: []`, but nothing forces it to. If your API is
> reachable by anyone you don't trust, set `NDIF_POSTGRES_URL` and issue keys.
> `priority` is left as the client sent it when auth is off (only the auth-on
> branch stamps it), defaulting to False.

### Who may deploy a `trust_remote_code` key

`Processor.start` (`src/ndif/services/api/queue/processor.py`), when
`get_deployment` reports no replicas, refuses a key whose `trust_remote_code`
field is set (`src/ndif/common/types.py` `key_field`) unless some queued request
has `trust_remote_code`, raising `DeploymentError` before `Replica.provision`.
The handler sends the
[`Could not deploy this model` refusal](../reference/error-messages.md#error-responses)
on every queued request's response stream and purges the queue.
[Auth and Limits](../concepts/auth-and-limits.md#trust_remote_code) has the
full rule; the key field is in
[Models and Deployment](../operating/models-and-deployment.md#trust_remote_code-in-the-model-key).

**`validate_client_versions`** (`src/ndif/services/api/versioning.py`) is called
inside the `/request` body, not as a dependency. It compares the client's
`nnsight-version` / `python-version` headers against `MIN_NNSIGHT_VERSION` /
`MIN_PYTHON_VERSION`, read from `NDIF_MIN_NNSIGHT_VERSION` /
`NDIF_MIN_PYTHON_VERSION` once at import. An unset minimum skips that check
entirely; otherwise a missing, malformed, or too-old version is a 400. Only
major.minor of the python version is compared.

## The request path

```mermaid
flowchart TB
  C["nnsight client"]
  subgraph W["gunicorn worker (uvicorn)"]
    D1["require_ray_connection<br/>GET ray:connected"]
    D2["validate_request<br/>parse data + verify_api_key<br/>stamp email/trusted/trust_remote_code/priority"]
    B["create_request body<br/>blob.read() -> payload<br/>response(RECEIVED)"]
    S["/subscribe websocket"]
  end
  R[("Redis")]
  DP["Dispatcher process"]
  MA["model actor (Ray)"]
  OS[("Object store")]

  C -- "WS /subscribe" --> S
  S -- "{session_id}" --> C
  C -- "POST /request (multipart)" --> D1 --> D2 --> B
  B -- "LPUSH queue &lt;pickle&gt;" --> R
  B -- "200 RECEIVED" --> C
  R -- "BRPOP queue" --> DP
  DP -- "handle.run.remote(request)" --> MA
  MA -- "PUBLISH session_id &lt;json or pickled&gt;" --> R
  R -- pubsub --> S -- "status updates, small results" --> C
  MA -- "put large result blob" --> OS
  C -- "GET presigned url" --> OS
```

### POST /request in detail

Every line of `create_request` (`src/ndif/services/api/app.py`) matters:

- `request.payload = await blob.read()` pulls the *entire*
  serialized-interventions blob into memory. No streaming, no size cap.
- The `ndif-timestamp` header (the client's send time) becomes a `SENT` bucket on
  `RequestStatusTimeMetric` — client→server transit — but only when
  `received_at >= sent_at`, so obvious clock skew is dropped.
- `request.response(Status.RECEIVED, ...)` advances the status *without
  publishing anything*. `BackendRequestModel._advance_status`
  (`src/ndif/common/schema/request.py`) seeds `last_status_time`, which doubles
  as the ingress timestamp and travels with the pickled request so the next hop
  (QUEUED) can bill the gap.
- `async_bytes_client.lpush(QUEUE_CONFIG.queue_key, pickle.dumps(request))` is
  the entire handoff. `LPUSH` plus the dispatcher's `BRPOP` gives FIFO. The
  binary client is required — `async_client` has `decode_responses=True` and
  would mangle the pickle.
- `RequestSizeMetric` gets the payload size plus the caller's IP (read only here)
  and user-agent.

The `BackendResponseModel` returned over HTTP is the *only* status the client
gets synchronously; everything after is pubsub or object store.

## Responses out

`BackendRequestModel.respond` / `arespond` (`src/ndif/common/schema/request.py`)
is the single fan-out point: Redis pub/sub on the `session_id` channel for a
blocking job, the object store at `responses/{request_id}.json` (read back by
`GET /response/{id}`) for a non-blocking one. The mechanics are in
[The Redis Layer](redis-layer.md#response-publishing).

Nothing in the API writes a response itself except the RECEIVED one it returns.
QUEUED / PROVISIONING / DEPLOYING come from the Processor, DISPATCHED from the
Replica, RUNNING / LOG / COMPLETED / ERROR from the model actor
(`src/ndif/services/ray/deployments/modeling/base.py`
`BaseModelDeployment.run`).

### The websocket

`subscribe` (`src/ndif/services/api/app.py`) accepts, mints
`session_id = uuid.uuid4().hex`, subscribes to the Redis channel of that name on
the bytes client, and *only then* sends `{"session_id": ...}`. Subscribing before
handing out the id closes a lost-message race: the client only POSTs after
receiving the id, so the channel is live before any status can be published.
A message starting with `{` is forwarded as a text frame; anything else is a
pickled response carrying the result and goes as a binary frame.

Then two tasks race: `forward()` pumps `pubsub.listen()` to the socket, and
`watch_disconnect()` sits in `websocket.receive()` (the client never sends, so it
returns only on disconnect). `asyncio.wait(..., FIRST_COMPLETED)` tears down
whichever loses; the `finally` cancels both and
`gather(..., return_exceptions=True)`s them, because `CancelledError` is a
`BaseException` that a plain `except Exception` would let bubble out as a logged
"Exception in ASGI application".

## `/status` and `/env`

Both go through `_coalesced_fetch` (`src/ndif/services/api/app.py`): a Redis
cache the dispatcher refreshes on demand, with one refresh per miss no matter how
many callers. The handshake, TTLs and failure codes are in
[The Redis Layer](redis-layer.md#the-coalesced-cache).

## Where per-worker state lives

There is almost none, which is why multiple workers are safe. Per worker: the
version minimums read at import, the Redis client singletons, the boto3 clients
and `_bucket_ready`, the Postgres pool, the Loki and Influx shipper threads
created in `post_fork`, and any live websocket sessions. Queue / Processor /
Replica state is **not** here at all — it belongs to the dispatcher process.

A client's `/subscribe` socket and its `POST /request` can land on different
workers. That is fine: the only link between them is the `session_id` Redis
channel.

## Gotchas

> **Pickle on the wire.** `create_request` pickles a `BackendRequestModel` into a
> Redis list and `Dispatcher.get` (`src/ndif/services/api/queue/dispatcher.py`)
> unpickles it. Anyone who can write to the `queue` key gets code execution in
> the dispatcher process. Redis has no auth in the dev compose stack and its port
> is published to the host (compose service `redis`, `ports`).

> **`/response/{id}` takes no API key** (`get_response` in
> `src/ndif/services/api/app.py`). A request id is a uuid4 hex, but knowing one
> lets anyone read that job's latest response — including, on COMPLETED, the
> presigned result URL. `/whoami` also takes no required key: a missing or
> unknown key resolves to `{"email": null, "tags": []}`; with auth on, a malformed
> one returns 400.

> **`ray:connected` has no TTL.** If the dispatcher process dies outright, the
> flag stays set and `require_ray_connection` keeps passing — see
> [The Redis Layer](redis-layer.md#the-rayconnected-flag).

> **No request size limit.** `await blob.read()` buffers the whole payload, then
> pickles it into a Redis string. Redis's 512MB value ceiling is the effective
> limit; nothing in the API rejects an oversized blob earlier or more clearly. The
> size is measured (`RequestSizeMetric`) but never enforced.

> **Blocking calls must be wrapped.** `ObjectStoreProvider.get` is boto3
> (synchronous), so `/response/{id}` runs it through `asyncio.to_thread`, and
> `arespond` does the same for the object-store write. Metric emission is already
> non-blocking; S3 and the sync Redis client are not. Version gating is
> import-time — changing `NDIF_MIN_NNSIGHT_VERSION` needs a restart.

## Related

- [HTTP API](../reference/http-api.md) — the endpoint-by-endpoint reference.
- [Queue Internals](queue-internals.md) — what happens after the `LPUSH`.
- [Sandbox Execution](../concepts/sandbox-execution.md) — what `trusted` selects
  between.
- [Request Lifecycle](../concepts/request-lifecycle.md) — the same journey
  without the code.
- [Auth and Limits](../concepts/auth-and-limits.md) — the API-key model from the
  operator's side.
- [Schemas](../reference/schemas.md) — the `BackendRequestModel` fields.
- [The Redis Layer](redis-layer.md), [Redis Keys](../reference/redis-keys.md) —
  every key this service touches.
- [nnsight Integration](nnsight-integration.md) — the client half of the contract.
