---
title: Wire Schemas
one_liner: Every model in common/schema — BackendRequestModel, BackendResponseModel, Status, and the controller RPC shapes — field by field.
tags: [reference, api, controller, internals]
related: [docs/concepts/status-and-results.md, docs/concepts/request-lifecycle.md, docs/reference/http-api.md, docs/developing/nnsight-integration.md, docs/developing/controller-internals.md, docs/developing/sandbox-internals.md, docs/developing/telemetry-internals.md, docs/reference/redis-keys.md]
sources: [src/ndif/common/schema/request.py, src/ndif/common/schema/response.py, src/ndif/common/schema/controller.py, src/ndif/common/errors.py, src/ndif/common/types.py, src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/queue/processor.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/sandbox/driver.py, src/ndif/services/ray/sandbox/nns.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/controller.py]
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
it leaves the process. (`src/ndif/common/schema/__init__.py` re-exports
`BackendRequestModel`, `BackendResponseModel` and `Status`; the controller schemas
are imported by path.)

## `BackendRequestModel`

`src/ndif/common/schema/request.py` `BackendRequestModel` subclasses the nnsight
client's `RequestModel`, so it inherits the client's four fields and adds the
server-side ones.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model_key` | `str` | *(required)* | Identifies the served model, e.g. `nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", "revision": null, "task": "text-generation"}`. The queue routes on it; the actor loads from it. Inherited. |
| `session_id` | `str` | `""` | The Redis pub/sub channel the client's `/subscribe` websocket listens on. Empty means a **non-blocking** job — responses go to the object store instead. Inherited. |
| `compress` | `bool` | `False` | The payload is zstd-compressed *and* the server must compress the result blob. Inherited. |
| `env` | `dict[str, Any]` | `{}` | Per-request model environment (e.g. `{"peft": "<adapter repo id>"}`), applied with `model._remoteable_set_env` in `BaseModelDeployment.run` before execution. Inherited. |
| `id` | `str` | `uuid4().hex` | The job id: stamps every response, names the result object (`{id}.pt`), and is what a non-blocking client polls. Distinct from `session_id`. Minted server-side unless the envelope supplies one. |
| `api_key` | `Optional[str]` | `None` | Taken from the `ndif-api-key` header by `validate_request` when one is sent. |
| `email` | `Optional[str]` | `None` | The key owner's email, resolved at ingress so logs and metrics attribute to a person. With auth on, from Postgres; with auth off, whatever the client sent. |
| `trusted` | `bool` | `False` | **Selects the execution path** — see below. |
| `priority` | `bool` | `False` | Jump the queue. With auth on, from the key's `priority` user_tag (`Identity.priority`). Priority requests sort ahead as a *group*, FIFO within each group (`src/ndif/services/api/queue/request_queue.py` `RequestQueue.rank`); no aging, so saturated priority traffic starves normal requests. |
| `payload` | `Optional[bytes]` | `None` | The serialized interventions blob, filled from the multipart `blob` part by `create_request`. Not part of the client's JSON envelope. |
| `enqueued_at` | `Optional[float]` | `None` | Unix time the request joined a model's in-memory queue (`Processor.enqueue`); the autoscaler reads it to spot a stale queue. Preserved on a re-queue. |
| `last_status` / `last_status_time` | `Optional[Status]` / `Optional[float]` | `None` | The status the request is in and when it entered it. `last_status_time` is seeded at `RECEIVED`, so it doubles as the ingress time; per-stage latency comes from the `status_time` metric ([telemetry-internals.md](../developing/telemetry-internals.md)). |

### `trusted` — the flag that picks how user code runs

Stamped at ingress by `src/ndif/services/api/auth.py` `validate_request`: with
auth on, from the key's `trusted` user_tag (a client value is overwritten); with
auth off, a client-sent value stands and an unset one becomes `True`. Consumed by
`src/ndif/services/ray/sandbox/model.py` `SandboxModelDeployment.execute`: `True`
runs the block in the model actor process via `BaseModelDeployment.execute`;
`False` hands it to a runner subprocess (`SandboxHost.run_in_runner`), which
receives `(payload, compress, dtype, seed, env)` and drives the host's model over
a Unix socket. A base `ModelActor` ignores the flag and runs everything
in-process. The flag also rides into the deployment as `DeploymentConfig.trusted`
(below). The full story, including what each path exposes, is in
[auth-and-limits.md](../concepts/auth-and-limits.md) and
[sandbox-execution.md](../concepts/sandbox-execution.md).

### Payload, blobs, and encoding

`POST /request` is `multipart/form-data`: a form field **`data`** holding the
envelope as JSON, parsed with `BackendRequestModel.model_validate_json` in
`validate_request`, and a file part **`blob`** holding the serialized block — its
source text plus the globals and locals it references, pickled, zstd-compressed
when `compress` is set. The model is referenced by `model_key` and by persistent
ids the actor resolves to its live objects, never shipped. `RequestSizeMetric`
records `len(request.payload)`, the compressed size.

The actor inverts it with `BackendRequestModel.deserialize` — the subclass
override — in `BaseModelDeployment.execute`, and the sandbox runner calls the same
override (`src/ndif/services/ray/sandbox/nns.py` `_run`). The override classifies
an unreadable payload as `PayloadError` and an unresolvable module path as
`ArchitectureMismatchError` (`src/ndif/common/errors.py`), so both paths report
the same sentence. Past the API, the whole request travels as pickle:

| Hop | Encoding | Code |
|---|---|---|
| API worker → dispatcher | `pickle.dumps` onto the Redis list `NDIF_QUEUE_KEY`, `LPUSH`/`BRPOP` | `create_request`, `Dispatcher.get` |
| dispatcher → model actor | Ray serialization of `handle.run.remote(request)` | `src/ndif/services/api/queue/replica.py` `Replica.dispatch` |
| actor → result | `torch.save` with a CUDA→CPU relocating pickler, zstd level 3 if `compress` | `BaseModelDeployment.execute`, `BaseModelDeployment.prepare_result` |

> **Gotcha:** every server-side field is a declared field, so a client can put
> `id`, `email`, `priority` or `trusted` in the envelope. With auth on,
> `validate_request` overwrites `email`, `trusted` and `priority`; with auth off
> only an unset `trusted` is changed, and client-supplied `email`, `priority` and
> `id` survive.

### Response methods

All on the request, because it knows the id, the channel and the status clock
(`src/ndif/common/schema/request.py`):

- `response(status, description="", data=None, meta=None)` advances the status
  (`_advance_status`) and returns a `BackendResponseModel`, with no I/O.
- `respond(...)` also publishes, over sync Redis, from the model actor's threads;
  `arespond(...)` is the async twin the queue's workers use.
- Publishing branches on `session_id`. **Blocking** → `publish(session_id,
  response.model_dump_json())`, or for `pickled=True` a pipeline that `SET`s
  `response.pickle()` under `result:{id}` and publishes `RESULT_NOTICE` (`\x00`)
  plus that key; when the publish reached no subscriber the key is deleted at
  once. `/subscribe` tells them apart by the first byte, forwards JSON as a text
  frame, and `GETDEL`s a notice's key to send its bytes as a binary frame.
  **Non-blocking** → the latest non-`LOG` response is written as JSON to
  `responses/{id}.json` for `GET /response/{id}`; `pickled` is ignored there.

`_advance_status` is where telemetry hangs off the lifecycle: on a genuine
transition it records a `RequestStatusTimeMetric` point for the phase just left
and one structured event for the new status (WARNING for `ERROR`). `LOG` and
repeats return early, so they never move `last_status` or the clock.

## `Status`

A `str`-valued `Enum` defined by the nnsight client and re-exported unchanged, so
it serializes as its own name (`"COMPLETED"`).

| Value | Set by | Meaning | Kind |
|---|---|---|---|
| `RECEIVED` | `create_request` | Envelope parsed, key verified, blob read. Returned as the HTTP body of `POST /request`; never published on the channel. | once |
| `QUEUED` | `Processor.reply` (default) | In its model's in-memory queue; the description carries the 1-based position. | **repeatable** — re-sent as the position changes |
| `PROVISIONING` | `Processor.reply` | No replica exists; the processor is asking the controller to place one. | repeatable |
| `DEPLOYING` | `Processor.reply` | A replica exists but isn't serving yet. | repeatable |
| `DISPATCHED` | `Replica.dispatch` | Handed to a specific model actor over Ray. | once per dispatch |
| `RUNNING` | `BaseModelDeployment.run` | The actor has started executing the block. | once per dispatch |
| `COMPLETED` | `BaseModelDeployment.run` | Done; `data` carries the result or a URL to it. | **terminal** |
| `ERROR` | many (below) | Failed, cancelled, timed out, or killed; `description` carries the message. | **terminal** |
| `LOG` | `LogStream.write` (`src/ndif/services/ray/deployments/modeling/util.py`); in the sandbox, `SandboxDriver.next_event` through the `on_log` callback | One line of the user's `print()` output. Not a lifecycle stage. | **out-of-band, many per run** |

`ERROR` comes from the actor (an exception via `format_error`, or the execution
timeout — both in `BaseModelDeployment.run`); from the
dispatch side (`Replica.dispatch` when a dispatch fails or its worker is
cancelled, `Replica.cancel`); from `Processor.purge`; or from `ndif kill`
(`Dispatcher._kill`).

The transitions between these values, including the cold-start path
(`RECEIVED` → `PROVISIONING` → `DEPLOYING` → `QUEUED`), are drawn in
[Status and Results](../concepts/status-and-results.md#the-lifecycle).

The queue's `ProcessorStatus` (`UNINITIALIZED` / `PROVISIONING` / `DEPLOYING` /
`READY` / `CANCELLED`, in `src/ndif/services/api/queue/processor.py`) is a
different enum describing a model's replica pool; `Processor.reply` maps two of
its values onto the matching request statuses.

## `BackendResponseModel`

`src/ndif/common/schema/response.py` `BackendResponseModel` is an empty subclass
of the client's `ResponseModel`, so the bytes the backend publishes are exactly
what an unmodified client parses.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `id` | `str` | *(required)* | The request id this update belongs to. |
| `status` | `Status` | *(required)* | Lifecycle position (above). |
| `description` | `str` | `""` | Human-readable detail — queue position, error message, or one line of `print` output for `LOG`. |
| `data` | `Optional[Any]` | `None` | Only on `COMPLETED`: the result itself (on a response sent as `torch.save` bytes) when it fits under `RedisProvider.max_publish_bytes` and the job is blocking, otherwise a presigned GET URL for `{id}.pt` (`BaseModelDeployment.upload_bytes`). The cap lives in [status-and-results.md](../concepts/status-and-results.md). |
| `meta` | `Optional[dict]` | `None` | What the run cost, on `COMPLETED` **and** on the actor's `ERROR` responses (`src/ndif/services/ray/deployments/modeling/util.py` `request_meta`): `runtime` (wall-clock seconds), `max_mem_by_gpu` / `max_mem_pct_by_gpu` (bytes above the resident weights, and that against the headroom the request had), `max_memory_usage` (the worst device), and, only on an out-of-memory failure, `alloc_shortfall_by_gpu`. GPU keys are strings so the JSON and `torch.save` encodings agree. |

Model config (`arbitrary_types_allowed=True, protected_namespaces=()`) is
inherited. `pickle()` (a `torch.save` of `model_dump(exclude_unset=True)`) is
inherited and used by `respond(..., pickled=True)` for an inline result;
`unpickle()` is the client's half.

## Controller schemas

`src/ndif/common/schema/controller.py`. Both sides speak them — the API-side queue
calls, the Ray-side controller answers — as Ray call arguments and returns, not
JSON. `MODEL_KEY`, `REPLICA_ID` and `NODE_ID` are `str` aliases from
`src/ndif/common/types.py`.

### `DeploymentConfig` — deploy request

| Field | Type | Default | Meaning |
|---|---|---|---|
| `pinned` | `bool` | `False` | Exempt from autoscaling and cache eviction. |
| `replicas` | `int` | `1` | **Additive** — how many *new* replicas to place. Shrink with evict. |
| `trusted` | `bool` | `False` | Allow HuggingFace `trust_remote_code` for this deployment — see below. |
| `size_bytes` | `Optional[int]` | `None` | The weights in bytes, measured rather than estimated. Skips the Hub round-trip, so a deploy works with the Hub unreachable. Padding still applies. |
| `padding_factor` | `Optional[float]` | `None` | Multiplicative slack; overrides `NDIF_DEFAULT_PADDING_FACTOR`. |
| `padding_bias` | `Optional[int]` | `None` | Additive slack for per-process overhead (CUDA context, NCCL buffers, the runner pool); overrides `NDIF_DEFAULT_PADDING_BIAS`. |
| `gpus` | `Optional[int]` | `None` | Place on **exactly** this many GPUs. A count no tensor-parallel degree divides is refused. |
| `max_tp` | `Optional[int]` | `None` | Cap (or supply) the largest tensor-parallel degree; **`0` forces no tensor parallelism**. Inert unless `NDIF_TP_MODEL_ACTOR_CLASS` is set. |
| `execution_timeout_seconds` | `Optional[float]` | `None` | Per-request timeout; `None` uses the controller default. |
| `dtype` | `Optional[str]` | `None` | A torch dtype name or a quantization (`nf4`/`int4`/`4bit`, `fp4`, `int8`/`8bit`, `fp8`; see `ndif deploy --dtype`). Pinned to a concrete value by `_ControllerActor._deploy` so the estimate and the load match. |
| `actor_class` | `Optional[str \| type]` | `None` | Dotted import path or an `@ray.remote` class; `None` uses the controller's `default_model_actor_class`. |

The four placement overrides — `size_bytes`, `padding_factor`, `padding_bias`,
`gpus` — each let an operator supply one step of the size → placement derivation
directly instead of expressing "four cards" as a padding fudge factor.
`DeploymentConfig.normalize` coerces a bare key, a list or a dict into
`{model_key: DeploymentConfig}`. Built by `Replica.provision` (always
`replicas=1`), the CLI's deploy lib (`src/ndif/cli/lib/deploy.py`), and the
controller's `NDIF_DEPLOYMENTS` startup pins (`_ControllerActor.__init__`).

**`DeploymentConfig.trusted` is the request's `trusted` flag, one level up.** On
demand, `Replica.provision` passes `DeploymentConfig(trusted=processor.trusted)`,
where `processor.trusted` came from the request that started the model
(`Processor.ensure_started`). The controller threads it into the evaluator's size
estimate (`trust_remote_code=config.trusted` in `Cluster.deploy`) and the actor's
load (`BaseModelDeploymentArgs` in `_ControllerActor.apply`) — the two must agree.
So a trusted key both runs its block in-process and lets the model's own repo code
execute at load. The CLI sets it per model with `ndif deploy --trusted`.

### `ModelDeployResult` and `DeployResponse`

| Model | Field | Type | Default | Meaning |
|---|---|---|---|---|
| `ModelDeployResult` | `replicas` | `List[REPLICA_ID]` | `[]` | Replica ids placed **by this call**. |
| | `error` | `Optional[str]` | `None` | Why nothing (or not everything) was placed. |
| `DeployResponse` | `results` | `Dict[MODEL_KEY, ModelDeployResult]` | `{}` | Per-model outcome. |
| | `evictions` | `Set[Tuple[MODEL_KEY, REPLICA_ID]]` | `set()` | Replicas evicted to make room. |
| | `change` | `bool` | `False` | Any cluster state changed; the controller runs `apply()` only then. |

Built in `Cluster.deploy`, which guarantees each result has `replicas` or
`error`, so a caller checks only `error`. Consumed by `Replica.provision` and the
CLI.

### `ReplicaState` and `ReplicaStates`

`ReplicaStates` is `replicas: List[ReplicaState]` — the return type of both
`get_deployment` and `evict`. Each `ReplicaState` is
`ReplicaState(**deployment.get_state())` (`Deployment.get_state`).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model_key` | `MODEL_KEY` | *(required)* | Which model. |
| `replica_id` | `REPLICA_ID` | *(required)* | Cluster-unique replica id; part of the Ray actor name. |
| `deployment_level` | `str` | *(required)* | `"hot"` (on GPU) or `"warm"` (offloaded to CPU). |
| `gpus` | `Dict[int, int]` | *(required)* | GPU index → bytes budgeted on that device. |
| `size_bytes` | `int` | *(required)* | Estimated resident size used for placement. |
| `pinned` | `bool` | *(required)* | Exempt from eviction. |
| `node_id` | `Optional[str]` | `None` | Ray node hosting it. |
| `execution_timeout_seconds` | `Optional[float]` | `None` | Effective per-request timeout. |
| `actor_class` | `Optional[str]` | `None` | Dotted path of the serving actor class. |
| `deployed` | `float` | *(required)* | Unix time placed; the minimum-deployment-time guard counts from it. |

`_ControllerActor.get_deployment` lists **HOT replicas only**, which is what lets
the processor treat a cached model as not deployed. `Cluster.evict` returns the
pre-eviction snapshot of what it removed. `Processor.start` adopts the replicas
`get_deployment` lists; `Processor.reconcile` adopts replicas the controller has
gained and leaves dropped ones alone — the next dispatch to a gone replica raises
an eviction error that re-queues its request and drops the replica.

## Correspondence with the nnsight client schemas

These base classes are defined in the **nnsight** client package
(`nnsight.schema.request`, `nnsight.schema.response` — see
[nnsight.net](https://nnsight.net)), not in this repo, and a checkout may not
have a copy of nnsight next to it. The correspondence is *inheritance*, not
translation, and is spelled out here in full so you don't need the client source.

| Server | Client base | Relationship |
|---|---|---|
| `BackendRequestModel` | `RequestModel` — fields `model_key: str`, `session_id: str = ""`, `compress: bool = False`, `env: dict = {}`; methods `metadata()`, `serialize(tracer, compress)`, `deserialize(...)` | Subclass. Those four are the same fields. The rest of the request table is added server-side; the official client does not send it. `deserialize` is **overridden** to classify failures into `PayloadError` / `ArchitectureMismatchError`. |
| `BackendResponseModel` | `ResponseModel` — fields `id: str`, `status: Status`, `description: str = ""`, `data: Any \| None = None`, `meta: dict \| None = None`; methods `pickle()` / `unpickle()`, `__str__` | Subclass with **no added fields and no overrides**, so what the server publishes is exactly what the client validates. |
| `Status` | `Status` — a `str`-valued enum: `RECEIVED`, `QUEUED`, `PROVISIONING`, `DEPLOYING`, `DISPATCHED`, `RUNNING`, `COMPLETED`, `ERROR`, `LOG`, each with `value == name` | *The same enum object*, imported and re-exported — so a new status cannot be added server-side without a client release. |
| `DeploymentConfig`, `DeployResponse`, `ReplicaState(s)` | — | No client counterpart; purely internal to the API↔controller RPC. |

Where they intentionally differ:

- **Identity and trust are server-decided.** The official client sends the key
  only in the `ndif-api-key` header and none of `email`, `trusted` or `priority`.
  With auth on the server stamps all three from the key; with auth off it
  accepts what the envelope carries (the gotcha above).
- **The payload is out-of-band on the client, in-band on the server.** The client
  keeps the blob out of `RequestModel` and posts it as a separate multipart part;
  the server's `payload: Optional[bytes]` carries it through the queue and across
  Ray by pickle.
- **Status timing is server-only.** `last_status`, `last_status_time`, and
  `enqueued_at` let a request meter itself as it moves; the client never sees them.
- **`SENT` is not a `Status`.** The client's send time (`ndif-timestamp` header)
  is billed as a `status_time` point tagged `status="SENT"` (`create_request`), but no
  `Status.SENT` member exists and no response carries it.

## Related

- [status-and-results.md](../concepts/status-and-results.md) — the lifecycle from the user's side, response channels, presigned URLs; [request-lifecycle.md](../concepts/request-lifecycle.md) — one request end to end
- [http-api.md](./http-api.md) — the endpoints these models cross; [nnsight-integration.md](../developing/nnsight-integration.md) — the client/server contract and version coupling
- [controller-internals.md](../developing/controller-internals.md) — what the controller does with `DeploymentConfig`; [sandbox-internals.md](../developing/sandbox-internals.md) — what `trusted=False` actually runs
- [telemetry-internals.md](../developing/telemetry-internals.md) — the metrics and events `_advance_status` emits
