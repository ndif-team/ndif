---
title: Auth and Limits
one_liner: API keys, the Postgres-backed check, the trusted flag end to end (auth-off default, what it decides, which actor classes honor it, what the untrusted path cannot do), the trust_remote_code tag and the API gate on deploying a model that runs its own repo code, and every bound the code actually enforces on a request.
tags: [concepts, auth, api, gotchas]
related: [docs/concepts/request-lifecycle.md, docs/concepts/sandbox-execution.md, docs/concepts/deployments-and-eviction.md, docs/runbooks/enable-auth.md, docs/operating/production.md, docs/reference/env-vars.md, docs/reference/http-api.md, docs/developing/nnsight-integration.md]
sources: [src/ndif/services/api/auth.py, src/ndif/services/api/queue/processor.py, src/ndif/common/types.py, src/ndif/services/api/versioning.py, src/ndif/services/api/app.py, src/ndif/common/providers/postgres.py, src/ndif/common/schema/request.py, src/ndif/common/schema/controller.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/tp/model.py, src/ndif/services/ray/deployments/controller/controller.py, docker/postgres/init.sql]
---

# Auth and Limits

## What this covers

Who the server thinks you are, what that identity buys you, and every bound the
code actually enforces on a request. This is the one home for the `trusted`
flag and the `trust_remote_code` tag; other pages link here.

## The `trusted` flag

`request.trusted` is `"trusted" in request.tags`; the tags are stamped at
ingress by `validate_request` (`src/ndif/services/api/auth.py`). It decides two
things:

1. **Where the block runs.** On a sandbox actor class, a trusted block runs
   *inside the model actor process*, next to the weights; an untrusted one runs
   in a separate runner process driven over a Unix socket
   ([Sandbox Execution](sandbox-execution.md)).
2. **Whether it may deploy a model that runs its own repo code.** `trusted`
   implies the [`trust_remote_code`](#trust_remote_code) permission.

**How it is set.**

| Mode | `tags` on the request |
|---|---|
| Auth on (`NDIF_POSTGRES_URL` set) | the key's user_tags. A client-supplied value is overwritten. |
| Auth off, client sent `tags` | left as sent |
| Auth off, client sent nothing | `{"trusted"}` |

Auth is off unless `NDIF_POSTGRES_URL` is set, so a zero-config `just up` runs
every request that does not say otherwise as trusted: user code in-process, and
free to deploy a model that runs its own repo code. That is fine for a laptop and
not for anything reachable by other people; turning auth on is the switch — see
[Enable auth](../runbooks/enable-auth.md). To exercise the untrusted path in dev
without Postgres, send `tags: []`; ingress leaves it empty. See
[testing](../developing/testing.md).

**Which actor class honors it.** The fork lives in the actor class, not the
request path:

| Actor class | Untrusted request runs |
|---|---|
| `ModelActor` (the default when `NDIF_MODEL_IMPORT_PATH` is unset) | in-process — this class has no fork |
| `SandboxModelActor` (compose sets it) | in a runner process |
| `TPModelActor` (via `NDIF_TP_MODEL_ACTOR_CLASS`) | in-process on every rank — no fork |
| `SandboxedTPModelActor` (via `NDIF_TP_MODEL_ACTOR_CLASS`) | in one runner the whole group hosts |

So untrusted traffic is only separated from the model when the configured
classes are the sandboxed ones: `NDIF_MODEL_IMPORT_PATH` (or
`NDIF_DEFAULT_MODEL_ACTOR_CLASS`) set to
`ndif.services.ray.sandbox.model.SandboxModelActor`, and, if tensor parallelism
is on, `NDIF_TP_MODEL_ACTOR_CLASS` set to
`ndif.services.ray.tp.model.SandboxedTPModelActor`. Only the compose file sets
the first; nothing sets the second.

**What the untrusted path cannot do.** The runner holds a weightless meta model
and the backward pass runs on the host, so today a block on the untrusted path:

- cannot read weights — `model.lm_head.weight` is the runner's meta parameter,
  with a shape and no data;
- cannot edit weights — a write lands on the meta copy and is a no-op on the
  host;
- cannot train — an optimizer stepping `model.parameters()` updates meta tensors
  and changes nothing;
- cannot take gradients — `.grad` fails with `OutOfOrderError`.

Activations — reads, swaps, in-place edits, `.source`, module calls, caches —
behave as they do in-process. The mechanics are in
[Sandbox internals](../developing/sandbox-internals.md#what-the-untrusted-path-cannot-do).

**Observing the fork.** No log record carries `trusted`. Runners are child
processes of the model actor, one used per untrusted request and then replaced,
so new runner pids under the actor show a request took the untrusted path
([Enable auth](../runbooks/enable-auth.md#what-an-untagged-key-can-do) has the
command).

## `trust_remote_code`

A model key carrying `"trust_remote_code": true` loads the checkpoint's own
modelling code ([the key field](../operating/models-and-deployment.md#trust_remote_code-in-the-model-key)).
The API decides who may deploy one.

**The permission.** `request.trust_remote_code`
(`src/ndif/common/schema/request.py` `BackendRequestModel`) is `True` iff
`request.tags` has `trust_remote_code` or `trusted` (how the tags are stamped is
[above](#the-trusted-flag)). The `trust_remote_code` tag grants nothing else: the
key's blocks still run in the sandbox. Postgres needs its `user_tags` row
([Enable auth](../runbooks/enable-auth.md)).

**The gate.** `Processor.start` (`src/ndif/services/api/queue/processor.py`),
when the controller reports no replicas for the key, raises `DeploymentError` if
the key carries `trust_remote_code` and no queued request has
`request.trust_remote_code`. Every queued request gets the
[`Could not deploy this model` refusal](../reference/error-messages.md#error-responses)
and the queue is purged. Nothing else checks: a running model serves every
caller, and autoscaling, the controller's `scale`, the CLI and the dashboard
deploy without the check.

## How the check works

`validate_request` is a FastAPI dependency on `POST /request`. It parses the
multipart `data` field into a `BackendRequestModel` (422 on a bad envelope),
copies the `ndif-api-key` header onto it, and calls `verify_api_key`. Failures
short-circuit before the request is ever enqueued.

| Situation | Result |
|---|---|
| Postgres not configured | `verify_api_key` returns `None`; request allowed, `trusted` as in the table above, `trust_remote_code` equal to it |
| No key sent | `401` — "Missing or invalid API key" |
| Key present but not a UUID | `400` — key format message |
| Well-formed key not in `keys` | `403` — "Invalid API key" |
| Postgres unreachable or erroring | `503` — fail **closed**, never allow on a backend error |
| Key found | `Identity` returned; `email` and `tags` stamped on the request |

**A key is valid iff its row exists in the `keys` table.** The owning user's
`verification_status` is not checked — issuing and gating keys is the account
portal's job. The API connects as the read-only `ndifapi` role and only issues
`SELECT` (`docker/postgres/init.sql`). The query `LEFT JOIN`s through
`key_user_tag_assignments` to `user_tags`, so a known key with no tags still
returns one row — that is how "unknown key" differs from "known key, no tags".

`GET /whoami` (`src/ndif/services/api/app.py` `whoami`) resolves a key to
`{"email": ..., "tags": [...]}`. A missing or unknown key resolves to
`{"email": None, "tags": []}`, as does any key with auth off; with auth on, a
malformed key returns `400` and a Postgres failure `503`.

## What identity buys you

`Identity` carries `key_id`, `user_id`, `email`, and `user_tags`. Exactly three
tags mean anything to the server:

| Tag | Effect |
|---|---|
| `trusted` | `request.trusted` and `request.trust_remote_code` — see above |
| `trust_remote_code` | `request.trust_remote_code` — see [`trust_remote_code`](#trust_remote_code) |
| `priority` | `request.priority`: the request sorts ahead of all normal traffic for that model, FIFO against other priority requests. |

Everything else about identity is **observability only**. `email` travels with
the request across the Ray boundary so every downstream log line and metric
point can be attributed to a person. There is **no tier system, no quota, and no
per-model authorization**: any valid key may request any `model_key`.

## Client version gating

The nnsight client stamps `nnsight-version` and `python-version` headers on
every `POST /request`. Two env vars gate on them, both read **once at import**
(`src/ndif/services/api/versioning.py` `validate_client_versions`):

| Variable | Example | Unset behavior |
|---|---|---|
| `NDIF_MIN_NNSIGHT_VERSION` | `0.5.0` | no nnsight gating at all |
| `NDIF_MIN_PYTHON_VERSION` | `3.10` | no python gating at all |

When a minimum is set, three things produce a `400`: a missing header, an
unparseable version, and a version below the minimum. The python check compares
major.minor only. nnsight unpacks FastAPI's `{"detail": ...}` body and raises
`RemoteError` with it, so the user sees the sentence, not a bare status — the
same for every auth failure above.

> **Gotcha:** an empty string counts as unset, so `NDIF_MIN_NNSIGHT_VERSION=""`
> disables gating rather than rejecting everything. And because both are read at
> import, changing them means restarting the API.

## Request size

**There is no request size limit.** `create_request`
(`src/ndif/services/api/app.py`) reads the whole blob into memory, pickles the
request, and `LPUSH`es it into Redis; `RequestSizeMetric` records
`payload_bytes` but does not enforce anything. A large request is held in an API
worker's memory and then in Redis until the dispatcher pops it. If you need a
cap, put it in a reverse proxy in front of the API; there is no `NDIF_*` knob.

## The per-request execution timeout

`NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` is **unset by default**, meaning no
cap. Set it on anything shared — without a cap a runaway block holds its replica
for as long as it likes, and `NDIF_AUTOSCALING_MAX_REPLICAS` (3) of those take
the model out entirely. A deployment can override it per model via
`DeploymentConfig.execution_timeout_seconds`.

`BaseModelDeployment.run` (`src/ndif/services/ray/deployments/modeling/base.py`)
races the execution, on a thread of its own, against the timer and a cancel
event (`kill_switch`). On expiry it calls `interrupt()` and responds `ERROR` "Your
job exceeded the execution timeout of Ns."; a cancel does the same `interrupt()`
and either errors the request (`ndif kill`) or, for a HOT→WARM demotion,
re-queues it. The user's answer is bounded by the
timeout. What `interrupt` does to the running code depends on the class:

- **In-process (`ModelActor`, and trusted requests on the sandbox class).**
  `BaseModelDeployment.interrupt` calls `kill_thread`
  (`src/ndif/services/ray/deployments/modeling/util.py`), which injects
  `SystemExit` into the execution thread with CPython's async-exception API. It
  fires at that thread's next bytecode boundary, so a CUDA kernel or large tensor
  op already in flight finishes first. The thread is the request's own
  (`run_on_own_thread`), so a late injection cannot reach other work.
- **Untrusted on `SandboxModelActor`.** `SandboxModelDeployment.interrupt` stops
  the request's runner process (SIGTERM, then SIGKILL after 5s), which closes the
  socket the host thread is reading, then does the same `kill_thread`.
- **Tensor-parallel.** `TPModelDeployment.interrupt` sets an abort flag every
  rank checks at a shared checkpoint rather than killing a thread mid-collective;
  the sandboxed TP class then also stops the runner.

After any of these, the actor's per-request `cleanup` runs before it takes the
next request.

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
| Per-request execution time | `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` | unset (no cap) | only if you set it; see above for what the interrupt does |
| Per-actor GPU memory | controller's allocation, applied as `max_memory` + a per-process cap | per model | yes, for the actor process; a runner process is outside it |
| HTTP worker timeout | `NDIF_API_TIMEOUT` | 120 | yes — gunicorn, bounds the HTTP handler, not the job |

## Related

- [Sandbox Execution](sandbox-execution.md) — what the untrusted path provides,
  and what it does not.
- [Request Lifecycle](request-lifecycle.md) — where in the path each gate sits.
- [Enable auth](../runbooks/enable-auth.md) — turning Postgres-backed keys on
  from a working dev stack.
- [Production](../operating/production.md) — the rest of what changes when the
  deployment is reachable by other people.
