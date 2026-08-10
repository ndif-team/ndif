---
title: Wire Schemas
one_liner: Every model in common/schema — BackendRequestModel, BackendResponseModel, Status, and the controller RPC shapes — field by field.
tags: [reference, api, controller, internals]
related: [docs/concepts/status-and-results.md, docs/concepts/request-lifecycle.md, docs/reference/http-api.md, docs/developing/nnsight-integration.md, docs/developing/controller-internals.md, docs/developing/sandbox-internals.md, docs/developing/telemetry-internals.md, docs/reference/redis-keys.md]
sources: [src/ndif/common/schema/request.py, src/ndif/common/schema/response.py, src/ndif/common/schema/controller.py, src/ndif/common/types.py, src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/queue/processor.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/controller.py]
---

# Wire Schemas

## What this covers

`src/ndif/common/schema/` holds the data contracts every NDIF process speaks: the
request envelope a client POSTs, the status/result messages the server publishes
back, and the deploy/evict/get_deployment shapes the queue and the controller
exchange over Ray. The request and response models are *subclasses of the nnsight
client's own schema classes*, which is what makes the wire format the server emits
parse cleanly in an unmodified client. This page gives every field with its type,
default, and meaning; who builds it and who reads it; and how it is encoded when
it leaves the process. (`common/schema/__init__.py` re-exports three names —
`BackendRequestModel`, `BackendResponseModel`, `Status`; `controller.py` is
imported by path.)

## `BackendRequestModel`

`BackendRequestModel` (`src/ndif/common/schema/request.py:24`) subclasses the
nnsight client's `RequestModel`, so it inherits the client's four fields and adds
the server-side ones.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model_key` | `str` | *(required)* | Identifies the served model, e.g. `nnsight.modeling.LanguageModel:openai-community/gpt2`. The queue routes on it; the actor loads from it. Inherited from the client. |
| `session_id` | `str` | `""` | The Redis pub/sub channel the client's `/subscribe` websocket listens on. Empty means a **non-blocking** job — responses go to the object store instead. Inherited. |
| `compress` | `bool` | `False` | The payload is zstd-compressed *and* the server must compress the result blob it uploads. Inherited. |
| `env` | `dict[str, Any]` | `{}` | Per-request model environment (e.g. `{"peft": "<adapter repo id>"}`), applied via `model._remoteable_set_env` before execution (`base.py:294`). Inherited. |
| `id` | `str` | `uuid4().hex` | Fresh per request, minted server-side. Distinct from `session_id`: it identifies *this job*, stamps every response, names the result object (`{id}.pt`), and is the job id a non-blocking client polls. |
| `api_key` | `Optional[str]` | `None` | Copied off the `ndif-api-key` header by `validate_request` (`auth.py:165`), not sent in the JSON body by the client. |
| `email` | `Optional[str]` | `None` | The key owner's email, resolved once at ingress from Postgres and carried everywhere so logs/metrics attribute to a human. `None` when auth is off or the key has no user. |
| `trusted` | `bool` | `False` | **Selects the execution path** — see the callout below. |
| `priority` | `bool` | `False` | Jump the queue. Stamped at ingress from the key's `priority` user_tag (`Identity.priority`, `auth.py:80`). The queue sorts priority requests ahead of normal ones as a *group*, staying FIFO within each group (`request_queue.py`). Two priority groups, no further classes and no aging — so under saturated priority traffic normal requests wait indefinitely. |
| `payload` | `Optional[bytes]` | `None` | The serialized interventions blob. Filled from the multipart `blob` at `app.py:147`. **Never part of the JSON envelope.** |
| `enqueued_at` | `Optional[float]` | `None` | Unix time stamped when the request joins a model's in-memory queue (`processor.py:106`); the autoscaler reads it to spot a stale queue head. Preserved on a re-queue so the wait time stays honest. |
| `last_status` / `last_status_time` | `Optional[Status]` / `Optional[float]` | `None` | The status the request currently sits in and when it entered it. `last_status_time` is seeded at `RECEIVED`, so it doubles as the ingress timestamp — there is no separate "received at" field, and per-stage latency is reconstructed from the `status_time` metric (see [telemetry-internals.md](../developing/telemetry-internals.md)). |

### `trusted` — the flag that picks how user code runs

`trusted` (`request.py:57`, default `False`) is not an ordinary data field: it
selects the execution path.

**Stamped at ingress**, in the `validate_request` dependency: with auth on,
`request.trusted = identity.trusted` (`auth.py:178`) — `True` only if the caller's
API key carries the `trusted` user_tag; a client-supplied value is overwritten.
With auth off (`NDIF_POSTGRES_URL` unset, so `verify_api_key` returns `None`) the
client's own `trusted` is honored: if the request explicitly set it (checked via
`model_fields_set`, `auth.py:170`) that value stands, and only when it is left
unspecified does it default to `True` (`auth.py:184`). So an auth-off deployment
runs every request on the trusted path by default, but a caller can opt into the
sandbox path by sending `trusted: false`. `email` and `priority` are stamped only
in the auth-on branch.

**Consumed in the model actor.** `SandboxModelDeployment.execute`
(`services/ray/sandbox/model.py:232`) branches on it at `:242`. `trusted=True`
defers to `BaseModelDeployment.execute` (`modeling/base.py:379`), so the
deserialized block runs **in the actor process**, on a worker thread right next
to the loaded weights. `trusted=False` acquires a fresh runner **subprocess** from
the pool, sends it `(request.payload, request.compress)`, and drives it over a
Unix socket, servicing `INTERLEAVE` events so the model (still on the host,
weights never move) and the user's block take strict turns.

Sandboxing is still in progress, and the isolation is **process-based, not
VM-based** — the runner is a separate OS process without further hardening today;
the value is the seam. `src/ndif/services/ray/sandbox/ARCHITECTURE.md` is the
current reference. The flag also rides into the *deployment* as
`DeploymentConfig.trusted` — see [the controller schemas](#controller-schemas).

### Payload, blobs, and size accounting

Nothing about the payload is JSON. `POST /request` is `multipart/form-data` with
exactly two parts:

- a form field named **`data`** — the client's `RequestModel` as JSON, i.e. only
  `model_key`, `session_id`, `compress`, `env`. Parsed with
  `BackendRequestModel.model_validate_json` in `validate_request` (`auth.py:158`),
  so every server-only field takes its default.
- a file part named **`blob`** — the serialized execution payload. The client
  reduces the traced block to its *source text* plus only the globals/locals that
  source references, pickles that alongside the tracer, and zstd-compresses at
  level 6 when `compress` is set. Source rather than bytecode, so client and
  server need not share a Python version. The model is referenced by `model_key`
  and by persistent ids the actor resolves to its live objects, never shipped.

`create_request` reads the file part into `request.payload` (`app.py:147`) and
`RequestSizeMetric` records `payload_bytes = len(request.payload)` (`app.py:188`)
— the *compressed* size, since compression happens client-side. The actor inverts
it with the inherited `RequestModel.deserialize` in `BaseModelDeployment.execute`
(`modeling/base.py:392`). From there the whole `BackendRequestModel` — payload
included — travels as **pickle**, not JSON:

| Hop | Encoding | Code |
|---|---|---|
| API worker → dispatcher | `pickle.dumps` onto the Redis list `NDIF_QUEUE_KEY` (default `queue`), `LPUSH`/`BRPOP` for FIFO | `app.py:180`, `dispatcher.py:121` |
| dispatcher → model actor | Ray's own serialization on `handle.run.remote(request)` | `replica.py:203` |
| actor → result | `torch.save(saved, ...)` with a CUDA→CPU relocating pickler, zstd level 3 if `request.compress`, uploaded as `{request.id}.pt` | `base.py:424`, `base.py:546` |

> **Gotcha:** every server-side field is a *declared* field of the model, so a
> client can put `id`, `email`, `priority`, or `trusted` in the JSON envelope.
> `validate_request` overwrites `api_key`, `email`, `trusted`, and `priority`
> whenever auth resolves an identity (`auth.py:167`); with auth **off** only
> `trusted` is forced, and a client-supplied `email`, `priority`, or `id` survives.

### Response methods

Three, all on the request because the request knows the id, the channel, and the
status clock. `response(status, description="", data=None)`
(`request.py:75`) advances the status and returns a `BackendResponseModel`, with
no I/O; `respond` (`request.py:134`) also publishes, over sync Redis, from the
model actor's threads; `arespond` (`request.py:161`) is the async counterpart the
queue's workers use so a status update doesn't block the event loop. Publishing
branches on `session_id` (`request.py:148`):

- **blocking** (`session_id` set) → `publish(session_id, response.model_dump_json())`;
  the `/subscribe` websocket forwards the JSON string verbatim (`app.py:393`).
- **non-blocking** (`session_id == ""`) → the latest response is written to the
  object store at `responses/{id}.json` as `application/json` (`request.py:153`),
  which `GET /response/{id}` serves. `LOG` updates are skipped — no live stream.

`_advance_status` (`request.py:90`) is where telemetry hangs off the lifecycle:
on a *genuine* transition it emits a `RequestStatusTimeMetric` point for the phase
just left (`:112`) plus one structured `event()` for the new status (`:121`,
WARNING for `ERROR`). `LOG` and repeats return early (`:99`).

## `Status`

A `str`-valued `Enum` defined by the nnsight client and re-exported unchanged, so
it serializes as its own name (`"COMPLETED"`).

| Value | Set by | Meaning | Kind |
|---|---|---|---|
| `RECEIVED` | `create_request`, `app.py:174` | Envelope parsed, key verified, blob read, about to be pushed onto the Redis queue. Returned as the HTTP body of `POST /request`. | non-terminal, once |
| `QUEUED` | `Processor.reply` default, `processor.py:318` | Sitting in its model's in-memory queue; the description carries the 1-based position. | non-terminal, **repeatable** — re-sent as the position changes |
| `PROVISIONING` | `Processor.reply`, `processor.py:329` | No replica exists; the processor is asking the controller to place one. | non-terminal, repeatable |
| `DEPLOYING` | `Processor.reply`, `processor.py:332` | A replica exists but isn't serving yet — waiting on the actor to load weights. | non-terminal, repeatable |
| `DISPATCHED` | `Replica.dispatch`, `replica.py:199` | Handed to a specific model actor over Ray. | non-terminal, once |
| `RUNNING` | `BaseModelDeployment.run`, `base.py:261` | The actor has started executing the block. | non-terminal, once |
| `COMPLETED` | `BaseModelDeployment.run`, `base.py:370` | Done; `data` carries the presigned result URL. | **terminal** |
| `ERROR` | many (see below) | Failed, cancelled, timed out, or evicted mid-flight; `description` carries the message. | **terminal** |
| `LOG` | `LogStream.write`, `modeling/util.py:35`; `SandboxModelDeployment.next_event`, `sandbox/model.py:226` | One line of the user's `print()` output. Not a lifecycle stage. | **out-of-band, many times per run** |

`LOG` is the one a client sees repeatedly mid-run. In-process, a `LogStream`
stands in for `sys.stdout` and emits one `LOG` per complete line; in the sandbox,
the runner forwards each line as a `PRINT` event and `next_event`
(`sandbox/model.py:214`) echoes it as a `LOG` before waiting again for the reply
it actually wanted. Either way `_advance_status` ignores it (`request.py:99`), so
it never disturbs `last_status` or the status-time clock — and it is dropped for
non-blocking jobs, which have no live channel.

`ERROR` comes from an execution exception, timeout, or operator cancel in the
actor (`base.py:314`, `:322`, `:346`); a replica evicted or cancelled
mid-dispatch, or a dispatch failure (`replica.py:212`, `:255`, `:290`); a
processor purge (`processor.py:380`); or `ndif kill` (`dispatcher.py:346`).

```mermaid
stateDiagram-v2
    [*] --> RECEIVED: POST /request accepted
    RECEIVED --> QUEUED: pushed to the model's queue
    QUEUED --> PROVISIONING: no replica exists
    PROVISIONING --> DEPLOYING: replica placed, weights loading
    QUEUED --> DEPLOYING: replica exists, not ready
    QUEUED --> DISPATCHED: replica free
    DEPLOYING --> DISPATCHED: actor ready
    DISPATCHED --> RUNNING: actor started the block
    RUNNING --> COMPLETED: result uploaded, url in data
    RUNNING --> QUEUED: replica evicted mid-flight (re-queued at head)
    PROVISIONING --> ERROR: provision failed / purge
    DEPLOYING --> ERROR: start failed / purge
    DISPATCHED --> ERROR: dispatch failed / cancelled
    RUNNING --> ERROR: exception, timeout, cancel
    COMPLETED --> [*]
    ERROR --> [*]
```

`COMPLETED` and `ERROR` are the only terminal states; `QUEUED`, `PROVISIONING`,
and `DEPLOYING` may each be published repeatedly; `LOG` is out-of-band and can
arrive any number of times during any non-terminal status.

The queue's `ProcessorStatus` (`UNINITIALIZED` / `PROVISIONING` / `DEPLOYING` /
`READY` / `CANCELLED`) is a *different* enum describing a model's replica pool,
not a request; `Processor.reply` maps two of its values onto the matching request
statuses (`processor.py:326`).

## `BackendResponseModel`

`BackendResponseModel` (`src/ndif/common/schema/response.py:4`) is an empty
subclass of the nnsight client's `ResponseModel`. That is the whole point: the
bytes the backend publishes are exactly what an unmodified client parses.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | *(required)* | The request id this update belongs to. |
| `status` | `Status` | *(required)* | Lifecycle position (above). |
| `description` | `str` | `""` | Human-readable detail — the queue position, the error traceback, or one line of `print` output for `LOG`. |
| `data` | `Optional[Any]` | `None` | Only populated on `COMPLETED`, where it is the presigned GET url of the result blob. |

Model config: `arbitrary_types_allowed=True, protected_namespaces=()`, the latter
so `model_key`-style names don't collide with pydantic's `model_` namespace.
`pickle()` / `unpickle()` are inherited but unused — every response on the wire is
`model_dump_json()`.

**Result blobs are referenced, never embedded.** `upload_bytes`
(`modeling/base.py:536`) `torch.save`s the `nnsight.save()`-marked values,
optionally zstd-compresses them, `put`s them under the key `{request.id}.pt`, and
returns `ObjectStoreProvider.presigned_get(key)` (`base.py:549`–`:561`). That url
becomes `data` on `COMPLETED` (`base.py:370`); the client streams and
`torch.load`s it. Nothing large crosses the Redis channel.

## Controller schemas

`src/ndif/common/schema/controller.py`. These live in `common/` because both sides
speak them: the API-side queue calls, the Ray-side controller answers. They cross
as Ray call arguments/returns, not JSON. `MODEL_KEY`, `REPLICA_ID`, and `NODE_ID`
are `str` aliases from `common/types.py`.

### `DeploymentConfig` (`controller.py:20`) — deploy request

| Field | Type | Default | Meaning |
|---|---|---|---|
| `pinned` | `bool` | `False` | Exempt from autoscaling and cache eviction. |
| `replicas` | `int` | `1` | **Additive** — how many *new* replicas to place, regardless of what's running. Shrink with evict. |
| `trusted` | `bool` | `False` | Allow HuggingFace `trust_remote_code` for this deployment — see below. |
| `padding_factor` | `Optional[float]` | `None` | Overrides the controller's size-estimate padding. |
| `execution_timeout_seconds` | `Optional[float]` | `None` | Per-request execution timeout for this deployment; `None` uses the controller default. |
| `dtype` | `Optional[str]` | `None` | torch dtype name (`"bfloat16"`). Pinned to a concrete value by `_deploy` before evaluation (`controller.py:127`) so the estimate and the load match. |
| `actor_class` | `Optional[str \| type]` | `None` | Dotted import path resolvable inside the Ray actor, or an already-`@ray.remote` class; `None` uses `default_model_actor_class`. |

`DeploymentConfig.normalize()` (`controller.py:47`) coerces a bare model key, a
list of keys, or a dict into `{model_key: DeploymentConfig}`, so every deploy
entry point takes all three shapes. Constructed by `Replica.provision`
(`replica.py:103`, always `replicas=1`), the CLI's deploy lib
(`cli/lib/deploy.py:110`), and the controller's `NDIF_DEPLOYMENTS` startup pins
(`controller.py:91`).

**`DeploymentConfig.trusted` is the request's `trusted` flag, one level up.** When
a request provisions a model on demand, `Replica.provision` passes
`DeploymentConfig(replicas=1, trusted=processor.trusted)` (`replica.py:103`),
where `processor.trusted` came from the request that kicked the deployment off
(`Processor.ensure_started`, `processor.py:153`). The controller threads it into
the evaluator's size estimate (`trust_remote_code=config.trusted` in
`Cluster.deploy`, `cluster.py:169`) *and* into the actor's model load
(`trust_remote_code=deployment.trusted` in the `BaseModelDeploymentArgs` built by
`Controller.apply`, `controller.py:280`) — the two must agree, or the memory
accounting that placed the replica won't match what loads. So a `trusted` API key
does two things: it lets the block run in-process, and it lets the model's own
repo code execute at load. The CLI sets it per model, independent of any request.

### `ModelDeployResult` (`controller.py:61`) and `DeployResponse` (`controller.py:73`)

| Model | Field | Type | Default | Meaning |
|---|---|---|---|---|
| `ModelDeployResult` | `replicas` | `List[REPLICA_ID]` | `[]` | Replica ids placed **by this call** only. |
| | `error` | `Optional[str]` | `None` | Why nothing (or not everything) was placed. |
| `DeployResponse` | `results` | `Dict[MODEL_KEY, ModelDeployResult]` | `{}` | Per-model outcome. |
| | `evictions` | `Set[Tuple[MODEL_KEY, REPLICA_ID]]` | `set()` | Replicas evicted to make room. |
| | `change` | `bool` | `False` | Any cluster state changed — the controller only runs `apply()` when this is `True` (`controller.py:138`). |

`Cluster.deploy` guarantees each result has either `replicas` populated *or*
`error` set, so a caller only checks `error` (`cluster.py:262`). Built at
`cluster.py:159`–`:266`; consumed by `Replica.provision` (`replica.py:102`) and
the CLI.

### `ReplicaState` (`controller.py:91`) and `ReplicaStates` (`controller.py:109`)

`ReplicaStates` is just `replicas: List[ReplicaState]` (default `[]`) — the return
type of both `get_deployment` and `evict`. Each `ReplicaState` is built as
`ReplicaState(**deployment.get_state())` (`Deployment.get_state`, `deployment.py:112`).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model_key` | `MODEL_KEY` | *(required)* | Which model. |
| `replica_id` | `REPLICA_ID` | *(required)* | Cluster-unique replica id; also the Ray actor name component. |
| `deployment_level` | `str` | *(required)* | `"hot"` (on GPU) or `"warm"` (offloaded to CPU). |
| `gpus` | `Dict[int, int]` | *(required)* | GPU index → bytes budgeted on that device. |
| `size_bytes` | `int` | *(required)* | Estimated resident size used for placement accounting. |
| `pinned` | `bool` | *(required)* | Exempt from eviction. |
| `node_id` | `Optional[str]` | `None` | Ray node hosting it. |
| `execution_timeout_seconds` | `Optional[float]` | `None` | Effective per-request timeout. |
| `actor_class` | `Optional[str]` | `None` | Dotted path of the serving actor class. |
| `deployed` | `float` | *(required)* | Unix time the replica was placed; the minimum-deployment-time guard is computed from it. |

`Controller.get_deployment` lists **HOT replicas only** (`controller.py:351`) — a
WARM replica is invisible to it, which is what lets the processor treat a cached
model as "not deployed". `Cluster.evict` returns the *pre-eviction* snapshot of
everything it removed (`cluster.py:280`). Both are consumed by `Processor.start`
(`processor.py:184`, adopt existing replicas) and `Processor.reconcile`
(`processor.py:355`, shed replicas the controller no longer lists).

## Correspondence with the nnsight client schemas

These base classes are defined in the **nnsight** client package
(`nnsight.schema.request`, `nnsight.schema.response` — see
[nnsight.net](https://nnsight.net)), not in this repo, and a checkout may not
have a copy of nnsight next to it. The correspondence is *inheritance*, not
translation, and is spelled out here in full so you don't need the client source.

| Server | Client base | Relationship |
|---|---|---|
| `BackendRequestModel` (`common/schema/request.py:24`) | `RequestModel` — fields `model_key: str`, `session_id: str = ""`, `compress: bool = False`, `env: dict = {}`; methods `metadata()`, `serialize(tracer, compress)`, `deserialize(blob, persistent_objects, compress)` | Subclass. Those four **are the same fields** — same names, types, defaults. Everything else in the request table above is added server-side and never sent by the client. The three methods are inherited unchanged; the actor calls `deserialize` at `modeling/base.py:392`. |
| `BackendResponseModel` (`common/schema/response.py:4`) | `ResponseModel` — fields `id: str`, `status: Status`, `description: str = ""`, `data: Any \| None = None`; methods `pickle()` / `unpickle()` | Subclass with **no added fields and no overrides** (a body with only a docstring), so the JSON the server publishes is exactly what the client validates. `pickle`/`unpickle` are inherited but unused. |
| `Status` | `Status` — `RECEIVED`, `QUEUED`, `PROVISIONING`, `DEPLOYING`, `DISPATCHED`, `RUNNING`, `COMPLETED`, `ERROR`, `LOG` | *The same enum object*, imported and re-exported rather than redefined — so a new status cannot be added server-side without a client release. |
| `DeploymentConfig`, `DeployResponse`, `ReplicaState(s)` | — | No client counterpart; purely internal to the API↔controller RPC. |

Where they intentionally differ:

- **Identity and trust are server-only.** `api_key`, `email`, `trusted`, and
  `priority` are never authored by the client (the key rides in the
  `ndif-api-key` header); the server stamps them at ingress.
- **The payload is out-of-band on the client, in-band on the server.** The client
  keeps the blob out of `RequestModel` and posts it as a separate multipart part;
  the server's `payload: Optional[bytes]` carries it through the queue and across
  Ray by pickle.
- **Status timing is server-only.** `last_status`, `last_status_time`, and
  `enqueued_at` let a request meter itself as it moves; the client never sees them.
- **`SENT` is not a `Status`.** The client's send time (`ndif-timestamp` header)
  is billed as a `status_time` point tagged `status="SENT"` (`app.py:163`), but no
  `Status.SENT` member exists and no response carries it.

## Related

- [status-and-results.md](../concepts/status-and-results.md) — the lifecycle from the user's side, response channels, presigned URLs; [request-lifecycle.md](../concepts/request-lifecycle.md) — one request end to end
- [http-api.md](./http-api.md) — the endpoints these models cross; [nnsight-integration.md](../developing/nnsight-integration.md) — the client/server contract and version coupling
- [controller-internals.md](../developing/controller-internals.md) — what the controller does with `DeploymentConfig`; [sandbox-internals.md](../developing/sandbox-internals.md) — what `trusted=False` actually runs
- [telemetry-internals.md](../developing/telemetry-internals.md) — the metrics and events `_advance_status` emits
