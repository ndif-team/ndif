---
title: HTTP API Reference
one_liner: Every route the NDIF API registers — method, path, auth, request and response shape, status codes, and who calls it.
tags: [reference, api]
related: [docs/developing/api-service.md, docs/reference/schemas.md, docs/concepts/status-and-results.md, docs/concepts/auth-and-limits.md, docs/errors/client-side-failures.md, docs/developing/nnsight-integration.md]
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
`verify_api_key` is a no-op unless `NDIF_POSTGRES_URL` is set (`auth.py:93-95`), so
the Auth column describes behavior *when auth is enabled*; with it disabled every
request is accepted and marked `trusted`. And **no route requires auth except
`/request`** — including `/response/{id}`, which returns a job's status and, on
COMPLETED, its presigned result URL.

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
(`app.py:106`) and return **503** whenever the `ray:connected` Redis flag is
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
  `BackendRequestModel` (`auth.py:161`). Four fields:

  ```json
  {"model_key": "...", "session_id": "", "compress": false, "env": {}}
  ```

  | Field | Type | Meaning |
  |---|---|---|
  | `model_key` | str | Which model to run against — `"{import.path.ClassName}:{repo_id}"`, e.g. `nnsight.modeling.language.LanguageModel:openai-community/gpt2` |
  | `session_id` | str | The id handed out by `/subscribe`. Empty means non-blocking |
  | `compress` | bool | The blob is zstd-compressed, and the result blob should be too |
  | `env` | dict | Per-request model environment (e.g. `{"peft": "<adapter repo id>"}`) |

- `blob` (file part): the serialized interventions, `application/octet-stream`.
  It is the traced block's *source text* plus the globals and locals it
  references, pickled (and zstd-compressed when `compress` is true). Read whole
  into memory (`app.py:150`); there is no size limit and no streaming.
  `app.py:147-149` carries a TODO for a size cap; nothing enforces one today.

The server ignores any other field in `data`. It sets `api_key`, `email`,
`trusted`, and `priority` itself from the verified key (`auth.py:172`-`179`),
`payload` from the uploaded blob, and mints `id`.

> **`trusted` matters.** With auth enabled, a request from a key carrying the
> `trusted` user_tag runs its Python **in-process in the model actor** and can
> deploy the model with `trust_remote_code`; anything else runs in a separate
> runner process. **With auth disabled (`NDIF_POSTGRES_URL` unset) a request is
> marked trusted by default**, but a client can send `trusted: false` to force the
> runner path — `client_set_trusted` (`auth.py:170`) is what distinguishes an
> explicit `false` from an unset field, and only the unset case is forced to
> `True` (`auth.py:180-184`). See `docs/concepts/auth-and-limits.md`.

**Response** — `200` with a `BackendResponseModel`:

```json
{
  "id": "3f2a...c1",
  "status": "RECEIVED",
  "description": "Your job has been received and is waiting to be queued.",
  "data": null
}
```

`id` is the job id — pass it to `/response/{id}` for a non-blocking job. Every
later status arrives over the websocket or the object store, never on this
response.

**Errors**

| Code | Cause |
|---|---|
| 400 | Malformed API key (not a UUID) (`auth.py:109-114`), or a client version below `NDIF_MIN_*` (`_reject`, `versioning.py:27-28`) |
| 401 | Auth enabled and no `ndif-api-key` header (`auth.py:98-102`) |
| 403 | Well-formed key that isn't in the `keys` table (`auth.py:129`) |
| 422 | `data` isn't valid JSON or doesn't validate as a `BackendRequestModel` (`auth.py:162-165`) |
| 500 | Anything unhandled (enqueue failure, provider error) — body is always `{"detail": "Internal server error."}` (`app.py:88-103`) |
| 503 | Ray not connected (`app.py:114-119`), or auth enabled and Postgres unreachable (`auth.py:126`) |

**Example.** Building `payload.bin` by hand is impractical — it is a pickle of a
captured nnsight trace — but the multipart shape is exactly this:

```bash
curl -X POST http://localhost:8001/request \
  -H "ndif-api-key: 00000000-0000-0000-0000-000000000000" \
  -H "ndif-timestamp: $(python -c 'import time;print(repr(time.time()))')" \
  -F 'data={"model_key":"nnsight.modeling.language.LanguageModel:openai-community/gpt2","session_id":"","compress":false,"env":{}}' \
  -F "blob=@payload.bin;type=application/octet-stream"
```

In practice you post it through the client:

```python
from nnsight import LanguageModel
model = LanguageModel("openai-community/gpt2")
with model.trace("hello", remote=True):
    hidden = model.transformer.h[5].output.save()
```

---

## `WS /subscribe`

Open a session and receive its status updates. **nnsight clients only** — this
is how a blocking job gets progress.

Protocol (`app.py:371-396`): the server accepts, mints `session_id = uuid4().hex`,
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
{"id": "3f2a...c1", "status": "RUNNING", "description": "Your job has started running.", "data": null}
```

| `status` | Emitted by |
|---|---|
| `RECEIVED` | API, on the HTTP response to `/request` |
| `QUEUED` | `Processor.enqueue` / `Processor.reply` |
| `PROVISIONING`, `DEPLOYING` | `Processor.reply` during `start()` |
| `DISPATCHED` | `Replica.dispatch` |
| `RUNNING` | model actor (`modeling/base.py:317`) |
| `LOG` | model actor — a transient message, not a lifecycle stage |
| `COMPLETED` | model actor (`modeling/base.py:459-461`); `data` is a presigned GET URL, or the result blob itself when it is under `NDIF_MAX_SOCKET_RESULT_BYTES` |
| `ERROR` | anywhere; `description` carries the user-facing message |

On `COMPLETED`, `data` is a presigned URL for `{request_id}.pt` in the object
store (`upload_bytes`, `modeling/base.py:688-698`). The client GETs it directly, decompresses if
`compress` was set, and `torch.load`s it — the result never passes through the
API. The nnsight client sends its `ndif-api-key` header on the handshake, but the
server does not read it: `/subscribe` is unauthenticated.

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
written to the object store at `responses/{id}.json`
(`_response_key`, `common/schema/request.py:16-17`); this endpoint reads that
object back (`app.py:316-318`).

**Response** — `200` with a JSON status update, identical in shape to a websocket
frame. Only the *latest* status is stored, so polling can skip intermediate
states. **404** means an unknown id or that the first response hasn't landed yet;
the nnsight client treats that as "still running" and re-polls.

No auth. Anyone with a job id can read its status and its presigned result URL.

```bash
curl http://localhost:8001/response/3f2a...c1
```

---

## `GET /status`

Cluster deployment status. **Operational, not for nnsight clients** — the CLI and
humans use it.

Serves the `status` Redis key, a JSON blob produced by `Controller.status()`
(`services/ray/deployments/controller/controller.py:555`): one entry per model
actor with its `application_state` and `deployment_level`, plus cluster resources
and COLD (downloaded but not deployed) models. TTL `NDIF_STATUS_TTL_S`, 60s. On a
cache miss (`_coalesced_fetch`, `app.py:205`) concurrent callers coalesce through
a `SET NX` lock; only the winner appends to the `status:trigger` stream and the
rest wait on the `status:ready` pub/sub channel, bounded by
`NDIF_STATUS_TIMEOUT_S` (60s).

**Response** — `200`, `application/json`, the cached blob byte-for-byte.

| Code | Cause |
|---|---|
| 503 | Ray not connected, or the dispatcher's refresh failed (`app.py:251`, `:268`) |
| 504 | No refresh arrived within `NDIF_STATUS_TIMEOUT_S` (`app.py:261`) |

> **Note:** the dashboard deliberately does **not** use this endpoint. Its
> `/api/status` (`services/dashboard/backend/routers/deploy.py:22-59`) hits the
> controller actor over Ray directly, to avoid showing a card that reads stale
> for up to a TTL right after a deploy or evict.

```bash
curl -s http://localhost:8001/status | jq .
```

---

## `GET /env`

The cluster's python version and installed packages, so a client can match its
local environment. Backs the CLI's `ndif env`
(`src/ndif/cli/commands/env.py:46-48`).

Structurally identical to `/status`: cached at the `env` Redis key with
`NDIF_ENV_TTL_S` (300s — the env only changes on a redeploy), refreshed by the
dispatcher's `env_worker` from `Controller.env()` (`controller.py:534`), bounded
by `NDIF_ENV_TIMEOUT_S` (60s). Same errors: 503 on a failed refresh or a
disconnected Ray, 504 on timeout.

**Response** — `200`, `application/json`. Keys of `packages` are *import* names
where resolvable, falling back to the distribution name
(`controller.py:542`-`552`).

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
is the JSON string `"pong"`. Used by `check_api` in the CLI (`src/ndif/cli/lib/checks.py`), which falls back
to a raw TCP check if it doesn't get a 200, and by the dev compose's `api`
healthcheck, which requests it with `python -c 'import urllib.request; ...'`
because the image ships no `curl` (`docker/docker-compose.yml:171-176`).

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
`HEAD` is registered alongside `GET` (`app.py:330-334`) so a monitor can poll it
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
(jump the model's queue). See `docs/concepts/auth-and-limits.md`.

Unlike every other key check, a bad key is **not** an error here: 401 (missing
or malformed) and 403 (unknown) are caught and turned into
`{"email": null, "tags": []}` (`app.py:357-362`). The same empty answer comes back
when auth is disabled entirely. A 503 (auth enabled, Postgres unreachable) is
the only failure that propagates.

```bash
curl -H "ndif-api-key: 00000000-0000-0000-0000-000000000000" \
  http://localhost:8001/whoami
```

## Related

- `docs/developing/api-service.md` — how these handlers are wired and what runs
  before them.
- `docs/reference/schemas.md` — `BackendRequestModel`, `BackendResponseModel`,
  `Status`.
- `docs/concepts/status-and-results.md` — the status lifecycle and the presigned
  result blob.
- `docs/concepts/auth-and-limits.md` — API keys, tags, version gating.
- `docs/errors/client-side-failures.md` — what each of these status codes looks
  like from inside nnsight.
