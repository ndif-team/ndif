---
title: Request Lifecycle
one_liner: One `model.trace(..., remote=True)` from the client's POST to the saved values landing back in the user's frame — every hop, and what breaks at each.
tags: [concepts, api, queue, ray, redis]
related: [docs/concepts/services-and-topology.md, docs/concepts/queue-and-scheduling.md, docs/concepts/status-and-results.md, docs/concepts/deployments-and-eviction.md, docs/concepts/sandbox-execution.md, docs/developing/queue-internals.md, docs/developing/model-actor.md, docs/developing/api-service.md, docs/reference/http-api.md]
sources: [src/ndif/services/api/app.py, src/ndif/common/schema/request.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/common/providers/objectstore.py]
---

# Request Lifecycle

## What this covers

The keystone page: one remote trace, end to end. Read it once and the rest of
the tree is annotations on this path.

Three facts frame the whole design:

1. **The model never moves; the code does.** The client builds its model on the
   meta device and ships a serialized *block* (source text plus the globals and
   locals it references) tagged with a `model_key`. The server holds the weights
   and runs the block against them. Nothing about the model is in the request.
2. **The API process cannot talk to Ray.** Only the queue dispatcher holds a Ray
   client connection. So the API's only job is to authenticate, validate, and
   `LPUSH` — every subsequent hop is a handoff through Redis or through Ray, and
   the client's status updates come back on a Redis pub/sub channel that the API
   merely forwards.
3. **Where the user's block executes depends on one boolean.** `request.trusted`
   is stamped at ingress from the API key. Trusted blocks run *in the model
   actor process*, next to the weights; untrusted blocks run in a separate
   runner process driven over a Unix socket. **With auth off — no
   `NDIF_POSTGRES_URL` — a client-supplied `trusted` is honored and an
   unspecified one defaults to `True`**, so a default `just up` runs all user
   code in-process, but a request sent with `trusted: false` still takes the
   runner path. See [Auth and Limits](auth-and-limits.md).

## The path

```mermaid
sequenceDiagram
    autonumber
    participant C as nnsight client
    participant A as API (gunicorn/FastAPI)
    participant R as Redis
    participant D as Dispatcher (spawned proc)
    participant K as Controller actor (Ray head)
    participant M as ModelActor replica (GPU)
    participant S as Object store (MinIO/S3)

    C->>A: WS connect /subscribe
    A->>R: SUBSCRIBE <session_id>
    A-->>C: {"session_id": ...}
    C->>A: POST /request (form data + blob file, ndif-api-key)
    A->>A: verify key, gate client version, read blob
    A->>R: LPUSH queue <pickled BackendRequestModel>
    A-->>C: 200 RECEIVED
    D->>R: BRPOP queue (+ drain up to 32)
    D->>D: Processor(model_key).enqueue(...)
    D->>R: PUBLISH <session_id> QUEUED
    R-->>C: QUEUED (via A's websocket)
    D->>K: get_deployment(model_key)
    alt no HOT replica
        D->>K: scale(model_key, 1, DeploymentConfig(trusted=...))
        D->>R: PUBLISH PROVISIONING / DEPLOYING
        K->>M: create actor, load weights onto assigned GPUs
    end
    D->>M: __ray_ready__ (poll until up)
    D->>R: PUBLISH DISPATCHED
    D->>M: run(request)  [Ray call]
    M->>R: PUBLISH RUNNING
    alt request.trusted
        M->>M: deserialize + run the block in-process, interleaved with the forward pass
    else untrusted
        M->>M: hand the blob to a fresh runner process; drive the forward pass over a socket
    end
    M->>R: PUBLISH LOG (per print line)
    alt result <= NDIF_MAX_SOCKET_RESULT_BYTES and session_id
        M->>R: PUBLISH COMPLETED carrying the blob itself
        R-->>C: COMPLETED (binary frame via A's websocket)
    else large, or non-blocking
        M->>S: PUT <request_id>.pt (torch.save, zstd)
        M->>R: PUBLISH COMPLETED + presigned url
        R-->>C: COMPLETED (via A's websocket)
        C->>S: GET presigned url
    end
    C->>C: decompress, torch.load, push saves into the frame
```

## Hop by hop

**1. Client serializes.** `RemoteBackend._serialize` registers non-installed
local modules for by-value pickling (`pull_env`), then
`RequestModel.serialize(tracer, compress)` reduces the traced block to source +
referenced names and cloudpickles it (zstd level 6 when `CONFIG.API.COMPRESS`).
The JSON envelope alongside it is small: `model_key`, `session_id`, `compress`,
and a per-request `env` dict (e.g. a PEFT adapter to swap in).

**2. Subscribe, then POST.** The client opens `ws://…/subscribe` *first* and
takes the server-minted `session_id` from the first frame; the API subscribes to
the Redis channel of that name before handing the id over
(`src/ndif/services/api/app.py:394`), so no update can be published before
anyone is listening. Only then does the client POST — multipart, with the JSON
envelope as the `data` form field and the block as the `blob` file.

**3. API ingress.** `POST /request` (`src/ndif/services/api/app.py:122`) runs
three gates before it does any work: `require_ray_connection` (503 if the
`ray:connected` Redis flag is absent), `validate_request` (parse the envelope,
verify the API key, stamp `email` / `trusted` / `priority` onto the request),
and `validate_client_versions` (`app.py:140`). Then it reads the blob into
`request.payload` (`app.py:150`), advances the request to `RECEIVED`, and
`LPUSH`es the *pickled* request onto the Redis list named by `NDIF_QUEUE_KEY`
(default `queue`, `app.py:183`). The `RECEIVED` response is what the HTTP call
returns; everything after this is asynchronous.

**4. Dispatcher pops.** The dispatcher is a separate spawned process started by
gunicorn's `on_starting` hook. Its loop `BRPOP`s the queue with a 10s timeout
and then drains up to 31 more with non-blocking `RPOP`
(`src/ndif/services/api/queue/dispatcher.py:141`) — so the shared list is FIFO
and one pop amortizes into a batch. Each request is routed by `model_key` to a
lazily created `Processor` (`dispatcher.py:162`).

**5. Processor queues and provisions.** `Processor.enqueue`
(`src/ndif/services/api/queue/processor.py:100`) hands the request to
`RequestQueue.put`, which stamps `enqueued_at` and orders by
`(group, prepend, enqueued_at)` — so a `priority` request sorts ahead of normal
traffic and stays FIFO against its peers. `enqueue` then calls `ensure_started`
and publishes `QUEUED` with the position the queue reports. If no
replica exists, `start()` asks the controller for existing replicas and adopts
them, or asks `controller.scale` for one more — the client sees `PROVISIONING`, then `DEPLOYING`.

**6. Controller places a replica.** `deploy` sizes the model on the meta device,
picks the best node/GPUs, evicts if it must, and creates a detached Ray actor
named `{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace. See
[Deployments and Eviction](deployments-and-eviction.md).

**7. Replica dispatches.** `Replica.wait` polls `__ray_ready__` until the actor
answers, then a worker task pulls from the shared per-model queue. For each
request it publishes `DISPATCHED` and makes the Ray call
`handle.run.remote(request)` (`src/ndif/services/api/queue/replica.py:228`).
The pickled `BackendRequestModel` — payload and all — crosses the Ray boundary
here.

**8. The actor runs the block.** `BaseModelDeployment.run`
(`src/ndif/services/ray/deployments/modeling/base.py:300`) refuses immediately
if its weights are on CPU (WARM), publishes `RUNNING`, applies `request.env` to
the model, and runs `execute` on a worker thread raced against
`execution_timeout` and a cancel event. `print()` inside the block becomes `LOG`
responses, one per line. `run` is a template — where `execute` actually runs the
block is the fork below.

**8a. The trusted fork.** The deployed actor class is
`NDIF_DEFAULT_MODEL_ACTOR_CLASS`; compose sets it to `SandboxModelActor`
(`src/ndif/services/ray/sandbox/model.py`). Its `execute`
(`SandboxModelDeployment.execute`, `sandbox/model.py:207`) is the whole fork:

```python
if request.trusted:
    return super().execute(request)
return self.run_in_runner(request, None)
```

`run_in_runner` (`SandboxHost.run_in_runner`, `sandbox/model.py:97`) takes a
runner from the pool, opens its socket, and sends the block plus everything the
runner needs to reproduce this actor's conditions:

```python
connection.send(
    (request.payload, request.compress, str(self.dtype), seed, request.env)
)
```

- **Trusted** — the base implementation runs: deserialize the block against the
  actor's live model, `tracer.execute(...)` interleaved with the real forward
  pass, collect the `nnsight.save()`-marked values by identity, `torch.save`
  them. All of that happens inside the model actor process.
- **Untrusted** — the actor takes a pre-warmed runner process from its pool
  (one fresh process per request, stopped afterwards, so nothing leaks between
  requests), ships it the blob, and services `INTERLEAVE` events: the block runs
  in the runner while the forward pass runs here, taking strict turns over the
  socket. The runner returns the `torch.save`d bytes directly.

Both paths produce the same bytes and both feed the same upload step. See
[Sandbox Execution](sandbox-execution.md).

**9. Result back, by one of two routes.** `prepare_result` (`base.py:662`)
compresses the `torch.save` output to match `request.compress` and meters it, so
the blob is byte-identical whichever route it takes. Then `run` picks
(`base.py:423`): if the request has a `session_id` and the blob is at or under
`NDIF_MAX_SOCKET_RESULT_BYTES` (20 MiB, `0` = no cap), it rides back *on the
response*; otherwise `upload_bytes` (`base.py:688`) PUTs it at `{request.id}.pt`
and presigns a GET valid for an hour. The `COMPLETED` response carries either
the bytes or the url in `data`, with `pickled` set when it is the bytes
(`base.py:460`) — that flag is what tells `/subscribe` to forward a binary
frame.

**10. Client collects.** Each published response goes to the Redis channel
named `session_id`; the API's `/subscribe` handler forwards it down the
websocket — a text frame for a JSON response, a binary one for a pickled
response carrying the result itself. On `COMPLETED` the client either takes the
bytes off the frame or streams the presigned url, decompresses,
`torch.load(..., map_location="cpu")`, and pushes the values into the caller's
frame so `h = ....save()` populates. A non-blocking
job has no `session_id`, so responses are written to `responses/{id}.json` in
the object store and polled via `GET /response/{id}` instead — see
[Status and Results](status-and-results.md).

## Where it can go wrong at each hop

| Hop | Failure | What the user sees | Where to look |
|---|---|---|---|
| 2 | Websocket never connects | client hangs / connection error before any status | [Compose networking](../gotchas/networking-and-compose.md) |
| 3 | Ray unreachable | `503` "compute backend is reconnecting" | [Services and Topology](services-and-topology.md) |
| 3 | Bad/missing API key | `401` / `400` / `403` from `POST /request` | [Auth and Limits](auth-and-limits.md) |
| 3 | Client too old | `400` naming the minimum version | [Client/server versions](../gotchas/client-server-versions.md) |
| 4 | Dispatcher process dead | request sits in Redis, no `QUEUED` ever arrives | [Queue internals](../developing/queue-internals.md) |
| 5 | Model never deploys | stuck on `PROVISIONING`/`DEPLOYING`, then `ERROR` | [Debug a stuck request](../runbooks/debug-a-stuck-request.md) |
| 6 | Won't fit on any node | `ERROR` mentioning `CANT_ACCOMMODATE` | [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) |
| 7 | Replica evicted mid-flight | silently re-queued at the front (no error) | [Deployments and Eviction](deployments-and-eviction.md) |
| 8 | Block raises | `ERROR` with the user's own traceback | [Server exceptions](../errors/server-exceptions.md) |
| 8 | Block exceeds the timeout | `ERROR` "exceeded the execution timeout of Ns" | [Auth and Limits](auth-and-limits.md) |
| 8 | Missing module in the block | `ModuleNotFoundError` at deserialize | [Client-side failures](../errors/client-side-failures.md) |
| 8a | Runner process dies mid-run | `ERROR` carrying the runner's formatted traceback | [Sandbox internals](../developing/sandbox-internals.md) |
| 9 | Object store unreachable | `ERROR` from the upload, after a successful run (only on the object-store route) | [Providers](../developing/providers.md) |
| 10 | Presigned url signed for the wrong host | `COMPLETED`, then a download failure client-side | [Compose networking](../gotchas/networking-and-compose.md) |
| 10 | `NDIF_MAX_SOCKET_RESULT_BYTES` raised or set to `0` | a large result exceeds Redis's pubsub output-buffer limit, the subscriber is dropped, and `COMPLETED` never arrives | [Status and Results](status-and-results.md) |

> **Gotcha:** the presigned url is an HMAC over the request *including the host*,
> so it must be signed with the address the client will actually hit.
> `NDIF_OBJECT_STORE_URL` is the server-side endpoint (`http://minio:9000`) and
> `NDIF_OBJECT_STORE_PUBLIC_URL` is the one used for signing
> (`http://localhost:9000` in compose). Getting these backwards produces jobs
> that complete and then fail to download.

## Related

- [Queue and Scheduling](queue-and-scheduling.md) — hops 4–5 in depth: one shared
  Redis list, per-model processors, and when a second replica appears.
- [Deployments and Eviction](deployments-and-eviction.md) — hop 6: what a
  deployment is, and what the controller is allowed to throw away to make room.
- [Status and Results](status-and-results.md) — hop 10: the full status lifecycle
  and why results go to a blob store.
- [Sandbox Execution](sandbox-execution.md) — hop 8 when the sandbox actor is in
  use, which it is by default in compose.
- [Queue internals](../developing/queue-internals.md) and
  [Model actor](../developing/model-actor.md) — the code behind hops 4–9.
