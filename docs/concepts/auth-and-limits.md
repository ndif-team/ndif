---
title: Auth and Limits
one_liner: API keys, the Postgres-backed check, and the one fact that matters most — with auth off a request is trusted by default (a client-supplied trusted is honored), which decides where user code runs and how models load.
tags: [concepts, auth, api, gotchas]
related: [docs/concepts/request-lifecycle.md, docs/concepts/sandbox-execution.md, docs/concepts/deployments-and-eviction.md, docs/runbooks/enable-auth.md, docs/operating/production.md, docs/reference/env-vars.md, docs/reference/http-api.md, docs/gotchas/client-server-versions.md]
sources: [src/ndif/services/api/auth.py, src/ndif/services/api/versioning.py, src/ndif/services/api/app.py, src/ndif/common/providers/postgres.py, src/ndif/common/schema/request.py, src/ndif/common/schema/controller.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/sandbox/model.py, docker/postgres/init.sql]
---

# Auth and Limits

## What this covers

Who the server thinks you are, what that identity buys you, and every bound the
code actually enforces on a request.

**Start here.** Auth is optional and off by default: nothing connects to
Postgres unless `NDIF_POSTGRES_URL` is set. With it unset, `validate_request`
takes this branch (`src/ndif/services/api/auth.py:180`):

```python
elif not client_set_trusted:
    # Auth is off (NDIF_POSTGRES_URL unset): a trusted-network / dev mode.
    # Default to trusted when the client didn't ask; an explicit request
    # value (True or False) is left untouched.
    request.trusted = True
```

Auth-off is a trusted-network / dev mode, not a blanket override: a
client-supplied `trusted` is **honored** (True *or* False), and only a request
that leaves it unspecified defaults to `True`. So a zero-config `just up` still
runs every request trusted — that default has two consequences, and they are the
highest-stakes facts in this documentation set for anyone self-hosting:

1. **User code runs inside the model actor process.** The sandbox actor's
   `execute` defers to the base implementation for a trusted request
   (`src/ndif/services/ray/sandbox/model.py:242`), so the submitted block is
   deserialized and run in the same process as the loaded weights, with no
   process boundary between it and the model.
2. **Models deploy with `trust_remote_code=True`.** The flag rides from the
   request through the queue's `DeploymentConfig` into the actor's model load
   (`src/ndif/services/ray/deployments/controller/controller.py:280`), so the
   model repo's own Python executes at load time.

A bare `just up` therefore runs everything trusted by default. That is fine for
a laptop and not fine for anything reachable by other people. Turning auth on is
the switch that separates the two — see
[Enable auth](../runbooks/enable-auth.md).

Because the auth-off default honors a client-supplied flag, you can exercise the
untrusted/sandbox path in dev without standing up Postgres: send `trusted: false`
in the request and ingress leaves it false, so the request takes the runner path.
See [docs/developing/testing.md](../developing/testing.md).

Trust is not only an ingress concept. A **dashboard** operator who deploys a model
is trusted for the same reasons: dashboard access is shell-equivalent trust on the
node, so a dashboard deploy loads with `trust_remote_code` and its requests run
in-process — see [the dashboard docs](../operating/dashboard.md).

## How the check works

`validate_request` (`src/ndif/services/api/auth.py:140`) is a FastAPI dependency
on `POST /request`. It parses the multipart `data` field into a
`BackendRequestModel` (422 on a bad envelope), copies the `ndif-api-key` header
onto it, and calls `verify_api_key`. Failures short-circuit before the request
is ever enqueued.

| Situation | Result |
|---|---|
| Postgres not configured | `verify_api_key` returns `None`; request allowed and defaulted to `trusted` unless the client explicitly set `trusted: false` |
| No key sent | `401` — "Missing or invalid API key" |
| Key present but not a UUID | `400` — key format message |
| Well-formed key not in `keys` | `403` — "Invalid API key" |
| Postgres unreachable or erroring | `503` — fail **closed**, never allow on a backend error |
| Key found | `Identity` returned; `email` / `trusted` / `priority` stamped on the request |

The validity criterion is deliberately narrow: **a key is valid iff its row
exists in the `keys` table.** The owning user's `verification_status` is *not*
checked here — issuing and gating keys is the account portal's job. The API
connects as the read-only `ndifapi` role and only ever issues `SELECT`
(`docker/postgres/init.sql`).

The query `LEFT JOIN`s through `key_user_tag_assignments` to `user_tags`, so a
known key with no tags still returns one row (tag `NULL`) — that's how "unknown
key" is distinguished from "known key, no tags".

## What identity buys you

`Identity` carries `key_id`, `user_id`, `email`, and `user_tags`
(`auth.py:66`). Exactly two tags mean anything to the server:

| Tag | Effect |
|---|---|
| `trusted` | The request's block runs in-process in the model actor, and any deployment it triggers loads with `trust_remote_code` |
| `priority` | The request sorts ahead of all normal traffic for that model, FIFO against other priority requests (with auth off there is no key to read it from, so `priority` is left as the client sent it — `False` unless the client set it) |

Everything else about identity is **observability only**. `email` is carried on
the request — across the Ray boundary via pickling — so every downstream log
line and metric point can be attributed to a person rather than an opaque key
(`src/ndif/common/schema/request.py:51`). `api_key` and `email` appear on the
request-size, status-time, execution-time, GPU-memory, and response-size
metrics.

There is **no tier system, no quota, and no per-model authorization** on the
request path: any valid key may request any `model_key`, and a key with no tags
is as capable as one with ten (minus `trusted`/`priority`).

`GET /whoami` resolves a key to `{"email": ..., "tags": [...]}`. It is
deliberately lenient — a missing, malformed, or unknown key resolves to
`{"email": None, "tags": []}` rather than erroring; only a 503 propagates
(`src/ndif/services/api/app.py:341`).

## Client version gating

The nnsight client stamps `nnsight-version` and `python-version` headers on
every `POST /request`. Two env vars gate on them, both read **once at import**
(`src/ndif/services/api/versioning.py:23`):

| Variable | Example | Unset behavior |
|---|---|---|
| `NDIF_MIN_NNSIGHT_VERSION` | `0.5.0` | no nnsight gating at all |
| `NDIF_MIN_PYTHON_VERSION` | `3.10` | no python gating at all |

When a minimum *is* set, three things produce a `400`: a missing header (treated
as "outdated nnsight, please upgrade"), an unparseable version, and a version
below the minimum. The python check compares major.minor only, dropping patch
and build noise.

A rejected client does not see a bare HTTP status — nnsight's `_post` unpacks
FastAPI's `{"detail": ...}` body and raises `RemoteError` with it, so the user
gets the actual sentence ("Client nnsight version 0.4.1 is below the minimum
supported 0.5.0. Please `pip install --upgrade nnsight`."). The same is true of
every auth failure above.

> **Gotcha:** an empty string counts as unset (`or None` in `versioning.py`), so
> `NDIF_MIN_NNSIGHT_VERSION=""` silently disables gating rather than rejecting
> everything. And because both are read at import, changing them means
> restarting the API.

## Request size

**There is no request size limit.** Nothing in the API, gunicorn config, or
compose file bounds the multipart body: `POST /request` reads the whole blob
into memory with `await blob.read()` (`app.py:147`), pickles the request, and
`LPUSH`es it into Redis. `RequestSizeMetric` records `payload_bytes` — it
measures, it does not enforce.

The practical consequences for a self-hosted deployment: a large request is held
in an API worker's memory and then in Redis's memory until the dispatcher pops
it, and a client with a large payload can consume both. If you need a cap, put
it in a reverse proxy in front of the API; there is no `NDIF_*` knob for it.

## The per-request execution timeout

`NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` (default `3600`) is the controller's
default, passed into each actor as `execution_timeout`; a deployment can
override it per model via `DeploymentConfig.execution_timeout_seconds`. The
actor enforces it by racing the execution against a timer
(`src/ndif/services/ray/deployments/modeling/base.py:298`):

```python
job = asyncio.create_task(asyncio.to_thread(self.execute, request))
kill = asyncio.create_task(self.kill_switch.wait())
with self.execution_scope(request):
    done, pending = await asyncio.wait({job, kill},
                                       timeout=self.execution_timeout,
                                       return_when=asyncio.FIRST_COMPLETED)
```

On expiry the actor calls `interrupt()` and responds `ERROR` with "Your job
exceeded the execution timeout of Ns." The same machinery serves operator
cancellation (`kill_switch`), which produces "Your job was cancelled or
preempted by the server."

How hard that interrupt lands depends on the fork:

- **Trusted (in-process).** `interrupt` injects `SystemExit` into the execution
  thread with CPython's async-exception API. It only fires at a bytecode
  boundary, so it cannot interrupt a CUDA kernel or a large tensor op already in
  flight — the timeout bounds when the *user* gets an answer, not necessarily
  when the GPU stops.
- **Untrusted (sandboxed).** `interrupt` also stops the runner process, which
  closes the socket and unblocks the host — a real kill of the code that was
  running.

## Every limit in one table

| Limit | Where | Default | Actually enforced? |
|---|---|---|---|
| API key required | `NDIF_POSTGRES_URL` set | unset (off) | yes, when configured |
| Min client nnsight version | `NDIF_MIN_NNSIGHT_VERSION` | unset | yes, when configured |
| Min client python version | `NDIF_MIN_PYTHON_VERSION` | unset | yes, when configured |
| Request body size | — | — | **no** — measured only |
| Requests per key / rate limit | — | — | **no** |
| Queue depth | — | — | **no** — unbounded |
| Replicas per model | `NDIF_AUTOSCALING_MAX_REPLICAS` | 3 | yes |
| Per-request execution time | `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | 3600 | yes, with the caveat above |
| Per-actor GPU memory | controller's allocation, applied as `max_memory` + a per-process cap | per model | yes |
| HTTP worker timeout | `NDIF_API_TIMEOUT` | 120 | yes — gunicorn, bounds the HTTP handler, not the job |

## Related

- [Sandbox Execution](sandbox-execution.md) — what the untrusted path actually
  provides today, and what it does not.
- [Request Lifecycle](request-lifecycle.md) — where in the path each gate sits.
- [Enable auth](../runbooks/enable-auth.md) — turning Postgres-backed keys on
  from a working dev stack.
- [Production](../operating/production.md) — the rest of what changes when the
  deployment is reachable by other people.
