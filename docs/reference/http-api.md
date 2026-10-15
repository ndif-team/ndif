---
title: HTTP API Reference
one_liner: Every route the NDIF API registers — method, path, auth, request and response shape, status codes, and who calls it.
tags: [reference, api]
related: [docs/developing/api-service.md, docs/reference/schemas.md, docs/concepts/status-and-results.md, docs/concepts/auth-and-limits.md, docs/reference/error-messages.md, docs/developing/nnsight-integration.md]
sources: [src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/api/versioning.py, src/ndif/common/schema/request.py, src/ndif/common/schema/response.py]
---

# HTTP API Reference

## What this covers

Every route `src/ndif/services/api/app.py` registers, enumerated from the source.
There is no `APIRouter` and no other module contributes routes — `app.py` is the
whole surface. The API listens on `NDIF_API_PORT` (8001), published as
`8001:8001` by the dev compose stack; examples below assume
`http://localhost:8001`.

Two things to know before reading the table. **Auth is off by default** —
`verify_api_key` (`src/ndif/services/api/auth.py`) is a no-op unless
`NDIF_POSTGRES_URL` is set, so the Auth column describes behavior *when auth is
enabled*; with it disabled every request is accepted, and one that doesn't set
`trusted` is marked trusted. And **no route requires auth except `/request`** —
`/response/{id}` included.

## All endpoints

| Method | Path | Auth | Audience | Purpose |
|---|---|---|---|---|
| `POST` | `/request` | key required (when enabled) | nnsight client | Submit a job |
| `WS` | `/subscribe` | none | nnsight client (blocking) | Open a session, stream its status updates |
| `GET` | `/response/{id}` | none | nnsight client (non-blocking) | Latest stored status for a job |
| `GET` | `/status` | none | CLI, humans | Cluster deployment status (cached) |
| `GET` | `/env` | none | CLI (`ndif env`) | Cluster python version + packages (cached) |
| `GET` | `/ping` | none | health checks, `ndif info`/`doctor` | Liveness |
| `GET`, `HEAD` | `/connected` | none | monitoring | Whether Ray is reachable |
| `GET` | `/whoami` | optional | login/config flows | Resolve an API key to an email + tags |

FastAPI also serves `/docs`, `/redoc`, and `/openapi.json` — nothing disables
them, so the interactive docs are live on any deployment.

`/request`, `/status`, and `/env` carry the `require_ray_connection` dependency
(`src/ndif/services/api/app.py` `require_ray_connection`) and return **503** whenever the `ray:connected` Redis flag is
absent, before any other work; `/connected` is that dependency and nothing else.
Every error body is `{"detail": ...}` — a string, or pydantic's error list for a
422.

---

## `POST /request`

Submit a job. This is the endpoint nnsight's `RemoteBackend` posts to.

**Headers**

| Header | Required | Meaning |
|---|---|---|
| `ndif-api-key` | when auth is enabled | The caller's key (a UUID) |
| `ndif-timestamp` | no | `repr(time.time())` at send; used for the `SENT` latency bucket. Ignored if unparseable or in the future |
| `nnsight-version` | only if `NDIF_MIN_NNSIGHT_VERSION` is set | Client nnsight version |
| `python-version` | only if `NDIF_MIN_PYTHON_VERSION` is set | Client `sys.version` (only major.minor is compared) |
| `user-agent` | no | Recorded on the `request_size` metric |

**Body** — `multipart/form-data` with exactly two parts:

- `data` (form field, string): the JSON-encoded request envelope, parsed into a
  `BackendRequestModel` by `validate_request`. The client sends four fields:

  ```json
  {"model_key": "...", "session_id": "", "compress": false, "env": {}}
  ```

  | Field | Type | Meaning |
  |---|---|---|
  | `model_key` | str | Which model to run against — the wrapper's import path, a colon, then its JSON identity, e.g. `nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", "revision": null, "task": "text-generation"}`. The `task` is part of the key, so the same repo under two tasks is two models |
  | `session_id` | str | The id handed out by `/subscribe`. Empty means non-blocking |
  | `compress` | bool | The blob is zstd-compressed, and the result blob should be too |
  | `env` | dict | Per-request model environment (e.g. `{"peft": "<adapter repo id>"}`) |

- `blob` (file part): the serialized interventions, `application/octet-stream`.
  It is the traced block's *source text* plus the globals and locals it
  references, pickled (and zstd-compressed when `compress` is true). Read whole
  into memory by `create_request`; there is no size limit and no streaming.

`data` is validated as a whole `BackendRequestModel`, so it may also carry that
model's server-side fields (unknown fields are ignored). `payload` always comes
from the blob part, and `api_key` from the `ndif-api-key` header when one is sent.
With auth enabled, `email`, `trusted` and `priority` are overwritten from the
verified key. With auth disabled they survive as sent, and an unset `trusted`
becomes `true` — so `trusted: false` is how a client exercises the sandbox
without Postgres. `id` is minted unless `data` supplies one. What `trusted`
decides is in [auth-and-limits.md](../concepts/auth-and-limits.md).

**Response** — `200` with a `BackendResponseModel`:

```json
{
  "id": "3f2a...c1",
  "status": "RECEIVED",
  "description": "Your job has been received and is waiting to be queued.",
  "data": null,
  "meta": null
}
```

`id` is the job id — pass it to `/response/{id}` for a non-blocking job. Every
later status arrives over the websocket or the object store, never on this
response.

**Errors**

| Code | Cause |
|---|---|
| 400 | Malformed API key (not a UUID; `verify_api_key`), or a client version below `NDIF_MIN_*` (`src/ndif/services/api/versioning.py` `validate_client_versions`) |
| 401 | Auth enabled and no key sent (`verify_api_key`) |
| 403 | Well-formed key that isn't in the `keys` table (`verify_api_key`) |
| 422 | `data` isn't valid JSON or doesn't validate as a `BackendRequestModel` (`validate_request`) |
| 500 | Anything unhandled (enqueue failure, provider error) — body is always `{"detail": "Internal server error."}` (`log_unhandled_exception`) |
| 503 | Ray not connected (`require_ray_connection`), or auth enabled and Postgres unreachable (`verify_api_key`) |

**Example.** Building `payload.bin` by hand is impractical — it is a pickle of a
captured nnsight trace — but the multipart shape is exactly this:

```bash
curl -X POST http://localhost:8001/request \
  -H "ndif-api-key: 00000000-0000-0000-0000-000000000000" \
  -H "ndif-timestamp: $(python -c 'import time;print(repr(time.time()))')" \
  -F 'data={"model_key":"nnsight.modeling.transformers.TransformersModel:{\"repo_id\": \"openai-community/gpt2\", \"revision\": null, \"task\": \"text-generation\"}","session_id":"","compress":false,"env":{}}' \
  -F "blob=@payload.bin;type=application/octet-stream"
```

In practice you post it through the client:

```python
from nnsight import TransformersModel
model = TransformersModel("openai-community/gpt2")
with model.trace("hello", remote=True):
    hidden = model.transformer.h[5].output.save()
```

---

## `WS /subscribe`

Open a session and receive its status updates. **nnsight clients only** — this
is how a blocking job gets progress.

Protocol (`src/ndif/services/api/app.py` `subscribe`): the server accepts, mints `session_id = uuid4().hex`,
subscribes to the Redis channel of that name, and sends `{"session_id": "a1b2..."}`
as the first message. The client echoes that id as `session_id` in a
`POST /request`; from then on every response published to that channel is
forwarded verbatim — as a **text** frame for a JSON response, or a **binary**
one for a `torch.save` response carrying the result itself. The socket closes
when the client disconnects or the forwarder errors. The client never sends anything on it.
Subscribe-before-id is deliberate — it guarantees the channel is live before any
status can be published, so no update is lost.

A status update is a JSON text frame:

```json
{"id": "3f2a...c1", "status": "RUNNING", "description": "Your job has started running.", "data": null, "meta": null}
```

| `status` | Emitted by |
|---|---|
| `RECEIVED` | API, on the HTTP response to `/request` |
| `QUEUED` | `Processor.enqueue` / `Processor.reply` |
| `PROVISIONING`, `DEPLOYING` | `Processor.reply` during `start()` |
| `DISPATCHED` | `Replica.dispatch` |
| `RUNNING` | model actor (`BaseModelDeployment.run`) |
| `LOG` | model actor — a transient message, not a lifecycle stage |
| `COMPLETED` | model actor (`BaseModelDeployment.run`); see below for `data` |
| `ERROR` | anywhere; `description` carries the user-facing message |

On `COMPLETED`, the result arrives one of two ways. If the result fits under the
inline cap (`RedisProvider.max_publish_bytes`) and the job is blocking, the whole
response is sent as one **binary** frame (`torch.save` bytes) with the result in
`data`. Otherwise `data` is a presigned GET URL for `{request_id}.pt` in the object
store (`BaseModelDeployment.upload_bytes`), which the client fetches directly. The
cap and the choice are explained in
[status-and-results.md](../concepts/status-and-results.md). The terminal
response (`COMPLETED` or `ERROR` from the model actor) also carries `meta`: the
run's wall-clock `runtime` and per-GPU memory peaks. `meta` appears only when both server and client run an nnsight newer than 0.8.0; the 0.8.0 and rc1 releases lack the field and pydantic drops it silently. `/subscribe` is
unauthenticated; the server ignores any key sent on the handshake.

```bash
# websocat: prints {"session_id": ...} then every status update
websocat ws://localhost:8001/subscribe
```

---

## `GET /response/{id}`

The latest stored status for a **non-blocking** job — what the nnsight client
polls when it submitted without a websocket.

`id` is the job id from the `POST /request` response.

A job with no `session_id` has no live channel, so each non-`LOG` response is
written to the object store at `responses/{id}.json` (`_response_key` in
`src/ndif/common/schema/request.py`); this endpoint (`get_response`) reads that
object back. A non-blocking result is always delivered by presigned URL. `LOG`
output is not stored, so a non-blocking job's `print()` output is lost.

**Response** — `200` with a JSON status update, identical in shape to a websocket
frame. Only the *latest* status is stored, so polling can skip intermediate
states. **404** means an unknown id or that the first response hasn't landed yet;
the nnsight client treats that as "still running" and re-polls.

No auth: anyone with a job id can read its status and its presigned result URL.

```bash
curl http://localhost:8001/response/3f2a...c1
```

---

## `GET /status`

Cluster deployment status. **Operational, not for nnsight clients** — the CLI and
humans use it.

Serves the `status` Redis key, a JSON blob produced by
`src/ndif/services/ray/deployments/controller/controller.py`
`_ControllerActor.status`: one entry per model actor with its `application_state`,
`deployment_level`, `model_key`, `task` and placement, plus cluster resources and
COLD (downloaded but not deployed) models. TTL `NDIF_STATUS_TTL_S`, 60s. On a
cache miss (`_coalesced_fetch`) concurrent callers coalesce through
a `SET NX` lock; only the winner appends to the `status:trigger` stream and the
rest wait on the `status:ready` pub/sub channel, bounded by
`NDIF_STATUS_TIMEOUT_S` (60s).

**Response** — `200`, `application/json`, the cached blob byte-for-byte.

Entries under `deployments` are keyed `{replica_id}:ModelActor:{model_key}` for a
live actor and by the bare repo id for a COLD model. A HOT entry carries
`application_state`, `deployment_level`, `model_key`, `replica_id`, `repo_id`,
`revision`, `task`, `config` (the HF config as a JSON string), `n_params`,
`size_bytes`, `gpus` (how many it spans), `dtype`, `pinned`, `trusted`,
`execution_timeout_seconds`, `actor_class`, and — unpinned, with a minimum
deployment time set — `schedule.end_time`. A WARM entry carries the subset from
`model_key` to `n_params`. The deployment-level `trusted` means the model was
loaded with `trust_remote_code`; it says nothing about whether a given request
runs trusted.

| Code | Cause |
|---|---|
| 503 | Ray not connected, or the dispatcher's refresh failed |
| 504 | No refresh arrived within `NDIF_STATUS_TIMEOUT_S` |

The dashboard's `/api/status` does not use this endpoint: it asks the controller
over Ray directly (`src/ndif/services/dashboard/backend/routers/deploy.py`
`status_endpoint`), so a card isn't stale for a TTL after a deploy or evict. The
dashboard's daily report cron does call it, to count hot models.

```bash
curl -s http://localhost:8001/status | jq .
```

---

## `GET /env`

The cluster's python version and installed packages, so a client can match its
local environment. Backs the CLI's `ndif env` (`src/ndif/cli/commands/env.py` `env`).

Structurally identical to `/status`: cached at the `env` Redis key with
`NDIF_ENV_TTL_S` (300s — the env only changes on a redeploy), refreshed by the
dispatcher's `env_worker` from `_ControllerActor.env`, bounded
by `NDIF_ENV_TIMEOUT_S` (60s). Same errors: 503 on a failed refresh or a
disconnected Ray, 504 on timeout.

**Response** — `200`, `application/json`. Keys of `packages` are *import* names
where resolvable, falling back to the distribution name.

```json
{"python_version": "3.11.9 (main, ...)", "packages": {"torch": "2.4.0", "nnsight": "0.5.0", "...": "..."}}
```

```bash
ndif env                # rendered
curl -s http://localhost:8001/env | jq .python_version
```

---

## `GET /ping`

Liveness, with no dependencies at all — it answers as long as a gunicorn worker
is alive, whether or not Ray, Redis, or Postgres are. **Response**: `200`, body
is the JSON string `"pong"`. Used by the CLI (`src/ndif/cli/lib/checks.py`
`check_api`, which falls back to a raw TCP check without a 200) and by the dev
compose's `api` healthcheck.

This is the right target for a container or load-balancer *liveness* check and
the wrong one for readiness: it answers 200 while Ray is still booting, so an
`api` container reports healthy minutes before `POST /request` stops 503ing.
`GET /connected` is the readiness signal; the compose healthcheck deliberately
uses `/ping` anyway so a slow GPU boot doesn't mark the API dead.

```bash
curl http://localhost:8001/ping   # "pong"
```

---

## `GET|HEAD /connected`

Whether the compute backend is reachable. The whole implementation is the
`require_ray_connection` dependency — reaching the body means the flag is set.
`HEAD` is registered alongside `GET` so a monitor can poll it
without a body.

**Response** — `200` `{"status": "connected"}`, or **503** with the detail
`"Service temporarily unavailable: compute backend is reconnecting. Please try
again in a few minutes."`

> **Gotcha:** this reflects the `ray:connected` Redis flag, which the dispatcher
> sets on connect and deletes while reconnecting — with no TTL. If the
> dispatcher process dies outright the flag survives and this reports
> "connected" while nothing is being dispatched.

```bash
curl -i http://localhost:8001/connected
```

---

## `GET /whoami`

Resolve an API key to its owner's email and tags. Used by login/config flows,
not by the job path.

**Headers** — `ndif-api-key` (optional).

**Response** — `200`:

```json
{"email": "someone@example.com", "tags": ["trusted", "priority"]}
```

`tags` are the key's `user_tags`. Two are meaningful to the server: `trusted`
(the request's code runs in-process in the model actor rather than a separate
runner, and the model may be deployed with `trust_remote_code`) and `priority`
(jump the model's queue). See [auth-and-limits.md](../concepts/auth-and-limits.md).

With auth enabled, a missing key (401) or an unknown one (403) is not an error
here: `whoami` turns both into `{"email": null, "tags": []}`. A malformed key
(not a UUID) is: it returns **400** with the same "Invalid API key format" detail
as `/request`. A 503 (Postgres unreachable) also propagates. With auth disabled
every call returns the empty answer.

```bash
curl -H "ndif-api-key: 00000000-0000-0000-0000-000000000000" \
  http://localhost:8001/whoami
```

## Related

- [api-service.md](../developing/api-service.md) — how these handlers are wired.
- [schemas.md](schemas.md) — `BackendRequestModel`, `BackendResponseModel`, `Status`.
- [status-and-results.md](../concepts/status-and-results.md) — the status lifecycle
  and how a result is delivered.
- [auth-and-limits.md](../concepts/auth-and-limits.md) — API keys, tags, version gating.
- [error-messages.md](error-messages.md) — what each of these looks like from
  inside nnsight.
