---
title: Client-Side Failures
one_liner: A user ran nnsight with remote=True and it broke — every symptom they can see, what it means server-side, how to confirm it, and how to fix it.
tags: [errors, api, auth, queue, gotchas]
related: [docs/errors/server-exceptions.md, docs/concepts/request-lifecycle.md, docs/concepts/status-and-results.md, docs/concepts/auth-and-limits.md, docs/reference/http-api.md, docs/developing/api-service.md, docs/developing/queue-internals.md, docs/operating/troubleshooting.md, docs/runbooks/debug-a-stuck-request.md, docs/runbooks/trace-a-users-failed-job.md, docs/gotchas/networking-and-compose.md, docs/gotchas/client-server-versions.md]
sources: [src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/api/versioning.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/common/providers/objectstore.py, src/ndif/common/schema/request.py]
---

# Client-Side Failures

## What this covers

Someone pasted an error from a `model.trace(..., remote=True)` run. This page
maps what they see to what the server did, in the order you should check it.

Two facts explain why almost every failure looks the same from the client:

1. **Nearly everything surfaces as `RemoteError`.** nnsight's `RemoteBackend`
   raises `RemoteError` both for a failed submission — unpacking FastAPI's
   `{"detail": ...}` body so the user sees the server's sentence, not a bare
   status code — and for any `ERROR` status that arrives on the response channel.
   The class carries no structure; the *text* is the diagnostic.
2. **`RECEIVED` is the only status that travels over HTTP.** Everything after it
   arrives on the `/subscribe` websocket (blocking) or via `GET /response/{id}`
   (non-blocking). So an HTTP status code always means the request was rejected
   at ingress and never entered the queue; an `ERROR` status always means it got
   past ingress.

## Symptom index

| What the user sees | Really means | Section |
|---|---|---|
| `RemoteError: Failed to send request: Missing or invalid API key...` | 401 — auth is on, no `ndif-api-key` header | [Submission rejected](#submission-rejected-http-status-codes) |
| `RemoteError: Failed to send request: Invalid API key format: '...'` | 400 — the key isn't a UUID | [Submission rejected](#submission-rejected-http-status-codes) |
| `RemoteError: Failed to send request: Invalid API key.` | 403 — well-formed key, no row in `keys` | [Submission rejected](#submission-rejected-http-status-codes) |
| `RemoteError: Failed to send request: Service temporarily unavailable: compute backend is reconnecting.` | 503 — the `ray:connected` flag is gone | [Submission rejected](#submission-rejected-http-status-codes) |
| `RemoteError: Failed to send request: Auth backend unavailable.` | 503 — Postgres is unreachable, auth fails closed | [Submission rejected](#submission-rejected-http-status-codes) |
| `RemoteError: Client nnsight version 0.4.1 is below the minimum supported 0.5.0...` | 400 from the version gate | [Version rejection](#version-rejection) |
| `RemoteError:` followed by a Python traceback in the user's own code | The block raised inside the actor or the runner | [ERROR with a traceback](#error-with-a-server-side-traceback) |
| `RemoteError: Your job exceeded the execution timeout of 3600s.` | The execution race timed out | [ERROR with a traceback](#error-with-a-server-side-traceback) |
| Status line sits on `QUEUED` (or `PROVISIONING`/`DEPLOYING`) forever | Waiting on a replica that never becomes ready | [Stuck in QUEUED](#stuck-in-queued) |
| `RemoteError: Error starting model...` / `Error provisioning model...` | The Processor's `start()` raised and purged the queue | [model_key provisions, then errors](#a-model_key-that-provisions-and-then-errors) |
| `COMPLETED`, then `httpx.ConnectError` / `403` on the download | The presigned URL was signed with a host the client can't reach | [COMPLETED but no result](#completed-but-the-client-cant-download-the-result) |
| The websocket closes and the call raises mid-run | The API restarted, or the client's connection dropped | [Websocket closes mid-run](#a-websocket-that-closes-mid-run) |
| `RemoteError: Replica was evicted while processing your request.` | Operator kill, reconcile, or a connection-error purge cancelled the worker | [Stuck in QUEUED](#stuck-in-queued) |

## Submission rejected: HTTP status codes

`_post` in nnsight's `RemoteBackend` calls `raise_for_status()`, reads the
`detail` field out of the body, and re-raises as
`RemoteError(f"Failed to send request: {detail}")`. So the user's message *is*
the server's message. Map it back like this:

| Code | Server detail | Raised at | Cause | Fix |
|---|---|---|---|---|
| 401 | `Missing or invalid API key. Please visit https://login.ndif.us/ ...` | `auth.py:98` | Auth is enabled (`NDIF_POSTGRES_URL` set) and no `ndif-api-key` header arrived | Set `CONFIG.API.APIKEY` / `NDIF_API_KEY` on the client |
| 400 | `Invalid API key format: '<key>'. API keys must be in the format: xxxxxxxx-...` | `auth.py:109` | The key isn't parseable as a UUID — usually a truncated paste or a placeholder | Re-copy the key |
| 403 | `Invalid API key.` | `auth.py:129` | A well-formed UUID with **no row in the `keys` table**. Validity is exactly "the row exists" — the owner's `verification_status` is not checked | Issue a key in the account portal, or check you're pointed at the right NDIF |
| 422 | pydantic error list | `auth.py:160`, `:162` | The `data` form field isn't JSON, or doesn't validate as a `BackendRequestModel`. Almost always a client/server schema mismatch | Upgrade nnsight; see [Version rejection](#version-rejection) |
| 503 | `Service temporarily unavailable: compute backend is reconnecting. Please try again in a few minutes.` | `app.py:115` | The `ray:connected` Redis flag is absent — the dispatcher is looping in `connect()` | Operator problem: [Troubleshooting](../operating/troubleshooting.md) |
| 503 | `Auth backend unavailable.` | `auth.py:126` | Postgres errored. Auth **fails closed** — a DB outage rejects every request rather than admitting unverified ones | Bring Postgres back, or unset `NDIF_POSTGRES_URL` to disable auth entirely |
| 500 | `Internal server error.` | `app.py:88` | Anything unhandled in ingress — a Redis write failure, a provider error | The real traceback is only in the API log (`just logs api`, logger `ndif.api`) |

Two follow-ups worth knowing:

- **401 vs 403 tells you whether auth is even on.** With `NDIF_POSTGRES_URL`
  unset, `verify_api_key` returns `None` immediately (`auth.py:94`) and *no* key
  check happens — so a 401 or 403 proves auth is configured. See
  [Auth and Limits](../concepts/auth-and-limits.md).
- **A 503 from `/request` is indistinguishable from a 503 from `/status`.** Check
  `GET /connected` — same dependency, no other work.

> **Gotcha:** `GET /connected` reads `ray:connected`, which has **no TTL**. The
> dispatcher sets it on connect and deletes it while reconnecting
> (`queue/dispatcher.py:85`, `:109`), so if the dispatcher process *dies* the flag
> survives and the API keeps reporting healthy while nothing is dispatched. A user
> whose job never leaves `RECEIVED` on a "healthy" server is usually this.

## Version rejection

`NDIF_MIN_NNSIGHT_VERSION` and `NDIF_MIN_PYTHON_VERSION` gate the
`nnsight-version` / `python-version` headers (`versioning.py:31`, `:52`). All
rejections are 400 and reach the user as `RemoteError`:

| Message | Meaning |
|---|---|
| `Client nnsight version was not provided. This usually means an outdated nnsight; please pip install --upgrade nnsight and retry.` | The header was missing. Old clients don't send it |
| `Malformed nnsight version '<v>'.` | Unparseable by `packaging.version.Version` — a source checkout with no installed distribution reports `""` |
| `Client nnsight version X is below the minimum supported Y. Please pip install --upgrade nnsight.` | Below the floor |
| `Client python version 3.9 is below the minimum supported 3.10. Please update python.` | Only major.minor is compared |

**Confirm from the server side:** `ndif env` prints the cluster's Python and
package versions, `ndif env --local` prints the client's. Drift in `nnsight`
between them is the usual root cause even when the gate is unset, because
serialization is a shared-type contract — a payload from a mismatched client can
fail at deserialize inside the actor instead, which surfaces as an
[ERROR with a traceback](#error-with-a-server-side-traceback).

> **Gotcha:** both minimums are read **once at import** (`versioning.py:23`) and
> an empty string counts as unset. Changing either needs an API restart.

## ERROR with a server-side traceback

The block ran and raised. `note()` in `RemoteBackend` turns any `ERROR` response
into `RemoteError(response.description)`, and for a user-code failure that
description is a fully formatted traceback pointing at **the user's own source
lines**, not server internals — the actor deserializes the block from its shipped
source text and registers it in `linecache` precisely so this works.

Which frames get stripped depends on the path:

- **Trusted (in-process).** `BaseModelDeployment.format_error` (`base.py:444`)
  runs nnsight's `clean_traceback` to drop nnsight plumbing, then
  `filter_traceback` to drop the actor's own wrapper frames.
- **Untrusted (sandboxed).** The runner formats the traceback in its own process
  and ships the *text* — tracebacks don't survive cloudpickle — and the host
  passes it through verbatim (`sandbox/model.py:188`).

Both are **user errors and never fatal to the replica**. Two descriptions are
not tracebacks at all:

| Description | Meaning |
|---|---|
| `Your job exceeded the execution timeout of {n}s.` | The race lost against `execution_timeout` (`base.py:324`). Default 3600s from `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` |
| `Your job was cancelled or preempted by the server.` | The kill switch fired — `ndif kill`, or an operator cancel (`base.py:316`) |

**Confirm:** `{environment=~".+"} | json | request_id=\`<id>\`` in Grafana, or
`just logs api` and the actor's log in the Ray dashboard. The full picture is in
[Server exceptions](server-exceptions.md), which classifies each exception by
whether it's the user's fault or the server's.

> **Gotcha:** a timeout does not necessarily stop the work. The in-process
> interrupt injects `SystemExit` into the execution thread, which CPython only
> delivers at a bytecode boundary — it cannot preempt a CUDA kernel already
> running. The sandboxed path additionally kills the runner process, which is a
> real stop.

## Stuck in QUEUED

The client renders each status as one updating line. Sitting on `QUEUED` means
the request reached the dispatcher and joined a per-model queue; sitting on
`PROVISIONING`/`DEPLOYING` means it is waiting for a replica.

| Observation | Cause | Confirm |
|---|---|---|
| Never even reaches `QUEUED` | The dispatcher isn't popping the Redis list | `redis-cli llen queue` is non-zero and growing |
| `QUEUED` with a rising position | Genuinely behind other work | `ndif queue` — check depth and `Replicas:` |
| `QUEUED` at position 1, forever | Autoscaling can't add a replica, or the only replica is wedged | `ndif queue` shows `ready` with a busy replica |
| `PROVISIONING`/`DEPLOYING` for many minutes | `Replica.wait` polls `__ray_ready__` **with no timeout** (`queue/replica.py:130`) | `ndif status` — is the actor `DEPLOYING` or absent? |
| `QUEUED` twice, no error in between | A replica was evicted mid-flight and the request was silently pushed back to the **front** of the queue (`replica.py:237`) | expected behavior, not a bug |

Autoscaling keys entirely off **head-of-line wait**, not depth: one request that
has waited past `NDIF_AUTOSCALING_WAIT_THRESHOLD_S` (30s) triggers a scale-up; a
hundred that all arrived a second ago do not. The cap is
`NDIF_AUTOSCALING_MAX_REPLICAS` (3).

Procedure: [Debug a stuck request](../runbooks/debug-a-stuck-request.md). Unstick
one with `ndif kill <request_id>`.

## A model_key that provisions and then errors

The request enters `PROVISIONING`, maybe `DEPLOYING`, then fails with one of:

```
RemoteError: Error provisioning model. Please try again later. Sorry for the inconvenience.
RemoteError: Error starting model. Please try again later. Sorry for the inconvenience.
```

Those strings come from `Processor.start` (`processor.py:180`, `:195`, `:203`);
whichever phase was current when the exception landed picks the wording. On a
failure before `READY` the Processor calls `purge()`, which errors **every**
queued request for that model — so several users see the same message at once.

**When the controller says why, that reason reaches the caller** as
`Could not deploy this model. <reason>` — a mistyped `repo_id`, a gated repo, or
`CANT_ACCOMMODATE` on a full cluster. These are the actionable cases, and
HuggingFace phrases most of them for end users already, naming the repo and the
page to visit. Note that "please try again later" would be wrong advice for all
three: each needs a different fix from the caller, and none of them is waiting.

The generic text above covers everything else — an internal fault whose text
would leak implementation detail and tell the caller nothing. For those the real
cause is server-side and only in the API log. The usual ones:

| Underlying cause | Where it shows |
|---|---|
| `CANT_ACCOMMODATE: placed N of M new replicas before the cluster ran out of room.` | The controller couldn't fit the model — [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) |
| `No GPU nodes available.` | `Cluster.nodes` is empty: the ray container has no GPU resource |
| An evaluator traceback (gated repo, bad repo id, needs `trust_remote_code`) | The controller's sizing pass failed |
| The actor died during weight load | `ndif status` shows `UNHEALTHY`, Ray restarts and it fails again |

The `trust_remote_code` case is the one that surprises people: a repo shipping
custom modelling code needs it merely to *build* the architecture, and a
deployment's trust level is fixed by **whichever request first deployed it**. See
[Controller internals](../developing/controller-internals.md).

## COMPLETED but the client can't download the result

The job succeeded. `data` on the `COMPLETED` response is a presigned GET URL, and
the client streams it directly from the object store — the result never passes
through the API. A failure here is almost always the URL's host.

```
httpx.ConnectError: [Errno -2] Name or service not known   # host 'minio' means nothing off the compose network
```

A presigned URL is an HMAC **over the request including the host**, so it must be
signed with the address the downloader will actually hit. Two variables:

| Variable | Compose value | Used for |
|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://minio:9000` | the server's own PUT/GET |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | `http://localhost:9000` | **signing** the client's GET (`objectstore.py:93`, `:164`) |

Set them backwards and every job completes and then fails to download. If
`NDIF_OBJECT_STORE_PUBLIC_URL` is unset entirely, `public_client` falls back to
`url` — which is right for a single-host install and wrong for compose.

Other download failures:

| Symptom | Cause |
|---|---|
| `403 SignatureDoesNotMatch` | The URL was signed with different credentials than the store now accepts, or a proxy rewrote the Host header |
| `403 Request has expired` | Presigned URLs expire after **one hour** (`objectstore.py:161`). A non-blocking job polled a day later gets a stale URL from `responses/{id}.json` |
| Download works, `torch.load` fails | A compression mismatch — the client decompresses only if it set `compress`; the actor compresses only if the request asked |

## A websocket that closes mid-run

The blocking client holds one `/subscribe` socket open for the whole job and
blocks in `recv()`. If it closes, the exception the user sees comes from the
`websocket` library, not from nnsight.

| Cause | Signature | What actually happened to the job |
|---|---|---|
| The API restarted | connection closed abnormally, no `ERROR` first | **The job is gone.** Restarting the API restarts the dispatcher with it, and every per-model queue and in-flight request is a plain Python object in that process |
| An idle proxy or load balancer timed out | closed after a long silent stretch | The job is still running; the client just isn't listening anymore. Nothing replays it |
| The client's process was interrupted | `KeyboardInterrupt` | Same — the server keeps going |

The consequential one is the first. Redis's `queue` list is the **only** durable
point in the whole path: once the dispatcher `BRPOP`s a request, it exists only
in that process's heap. `docker compose restart api` therefore drops every queued
and in-flight request, and a client on a blocking websocket gets **no further
status at all** — not even an `ERROR`, just silence. See
[Queue internals](../developing/queue-internals.md).

Nothing is stored for a blocking job, so there is no id to poll afterwards and no
way to recover the run. Re-submit.

For a job you want to survive a disconnect, submit non-blocking
(`remote=True, blocking=False`): each non-`LOG` response is written to
`responses/{id}.json` in the object store and `GET /response/{id}` reads the
latest one back. A 404 there means "no status recorded yet", which the client
treats as still-running — it does **not** distinguish an unknown id.

## Related

- [Server exceptions](server-exceptions.md) — the same failures from inside the
  server, exception by exception, with which ones are fatal to a replica.
- [Request Lifecycle](../concepts/request-lifecycle.md) — every hop, and the
  failure table indexed by hop.
- [Status and Results](../concepts/status-and-results.md) — the status enum, the
  two delivery routes, and why results are blobs.
- [HTTP API Reference](../reference/http-api.md) — the endpoints and their exact
  error codes.
- [Debug a stuck request](../runbooks/debug-a-stuck-request.md) and
  [Trace a user's failed job](../runbooks/trace-a-users-failed-job.md) — the
  procedures.
- [Troubleshooting](../operating/troubleshooting.md) — when the problem is the
  stack, not the request.
