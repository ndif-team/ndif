---
title: Error Messages
one_liner: Exact-text lookup for every error NDIF produces — what the user sees, what the server logs say, whose fault it is, and whether retrying helps.
tags: [errors, reference, api, queue, sandbox]
related: [docs/operating/troubleshooting.md, docs/runbooks/trace-a-users-failed-job.md, docs/runbooks/debug-a-stuck-request.md, docs/concepts/status-and-results.md, docs/concepts/request-lifecycle.md, docs/reference/http-api.md, docs/developing/model-actor.md, docs/developing/sandbox-internals.md, docs/developing/queue-internals.md, docs/developing/nnsight-integration.md]
sources: [src/ndif/common/errors.py, src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/api/versioning.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/deployments/modeling/util.py, src/ndif/services/ray/sandbox/driver.py, src/ndif/services/ray/sandbox/host.py, src/ndif/services/ray/sandbox/protocol.py, src/ndif/services/ray/sandbox/model.py, src/ndif/common/providers/ray.py, src/ndif/common/providers/objectstore.py, src/ndif/cli/lib/_common.py]
---

# Error Messages

Find the text a user pasted, or a line from the server logs, and read across.
For a whole-stack symptom (a service won't start, everything hangs) start at
[Troubleshooting](../operating/troubleshooting.md) instead.

## Read the shape first

- **A Python traceback through the user's own lines** — their code ran and
  raised. A user error, never fatal to the replica. Stop looking at the server.
- **One prose sentence, no frames** — the server refused or failed the request,
  and the sentence says which. A rejected payload is deliberately a sentence: every
  frame at that point is server-side, so a traceback would leak module layout and
  tell the caller nothing (`src/ndif/common/errors.py` `RequestError`).
- **`RemoteError: Failed to send request: ...`** — rejected at ingress with an
  HTTP status; the request never entered the queue. nnsight's `RemoteBackend`
  unpacks FastAPI's `{"detail": ...}` body, so the user sees the server's own
  sentence. Every status after `RECEIVED` arrives on the `/subscribe` websocket
  (blocking) or `GET /response/{id}` (non-blocking), never as an HTTP code.

When the text points at a serialization or architecture problem, compare versions
before anything else: `ndif env` (the cluster) against `ndif env --local` (the
client), or `from nnsight import ndif; print(ndif.compare())` on the client.

Grep the logs by request id:
`{environment=~".+"} | json | request_id=\`<id>\`` in Grafana, or `just logs api`.
Model-actor records carry `service="model"`, not `service="ray"`.

## Rejected at ingress

Logged by `src/ndif/services/api/app.py` `log_http_exception` as
`api request rejected: <code>` (logger `ndif.api`; 5xx at ERROR, 4xx at INFO), with
the detail as a field.

| Text the user sees (after `Failed to send request:`) | Code | Whose fault, and what to do | Retry? |
|---|---|---|---|
| `Missing or invalid API key. Please visit …` | 401 | User: auth is on and no `ndif-api-key` header arrived. Set `CONFIG.API.APIKEY` / `NDIF_API_KEY` on the client | after fixing |
| `Invalid API key format: '<key>'. API keys must be in the format: xxxxxxxx-…` | 400 | User: not a UUID — a truncated paste or a placeholder | after fixing |
| `Invalid API key.` | 403 | User: a well-formed key with no row in `keys`. Validity is exactly "the row exists" (`src/ndif/services/api/auth.py` `verify_api_key`) | after fixing |
| a pydantic error list, or `Invalid JSON: …` | 422 | Client/server schema mismatch on the `data` form field — upgrade nnsight (`src/ndif/services/api/auth.py` `validate_request`) | after fixing |
| `Service temporarily unavailable: compute backend is reconnecting. Please try again in a few minutes.` | 503 | Server: the `ray:connected` flag is absent, the dispatcher is reconnecting (`src/ndif/services/api/app.py` `require_ray_connection`) | yes |
| `Auth backend unavailable.` | 503 | Server: Postgres errored. Auth fails closed. Also logged as `API-key verification failed: Postgres unavailable` | yes |
| `Internal server error.` | 500 | Server: anything unhandled at ingress. The traceback is only in the API log, as `api unhandled error` (`log_unhandled_exception`) | maybe |

A 401 or 403 proves auth is configured: with `NDIF_POSTGRES_URL` unset,
`verify_api_key` returns before any key check.

### The client version gate

400s from `src/ndif/services/api/versioning.py` `validate_client_versions`, only
when `NDIF_MIN_NNSIGHT_VERSION` / `NDIF_MIN_PYTHON_VERSION` is set (see
[Configuration](../operating/configuration.md#the-client-version-gate)). User's
side in every case; retry after upgrading.

| Text the user sees | Meaning |
|---|---|
| ``Client nnsight version was not provided. This usually means an outdated nnsight; please `pip install --upgrade nnsight` and retry.`` | No `nnsight-version` header. Old clients don't send one; an nnsight run from a source tree with no installed distribution sends an empty one |
| `Malformed nnsight version '<v>'.` | Not parseable by `packaging.version.Version` |
| ``Client nnsight version X is below the minimum supported Y. Please `pip install --upgrade nnsight`.`` | Below the floor |
| `Client python version was not provided. …` | No `python-version` header |
| `Malformed python version '<v>'.` | Unparseable after major.minor is taken |
| `Client python version 3.9 is below the minimum supported 3.10. Please update python.` | Below the floor; only major.minor is compared |

## ERROR responses

The request was accepted and something failed later. `RemoteBackend` raises
`RemoteError(response.description)`, so the description is the text.

| Text the user sees | What the server logs say | Whose fault | Fatal / retry |
|---|---|---|---|
| A traceback through the user's own lines | `model execution errored` (`ndif.modeling`); `error_type` is the exception's class — on the sandboxed path the terminal exception carried by `RunnerError`, not the wrapper | User | Not fatal. Retry is pointless until the code changes. Two CUDA messages are the exception — [below](#the-two-fatal-cuda-errors) |
| `OutOfOrderError: '<location>' was requested but the model already ran past it` | as above | User: a block accessed modules out of forward-pass order | Not fatal |
| `ValueError: A barrier was never reached by every block it waits for; check the count it was created with` | as above | User: a `tracer.barrier(n)` whose `n` exceeds the blocks that call it | Not fatal |
| `Your request payload could not be read (<Type>: <detail>). It may be truncated or corrupted in transit, …` | the same sentence | User or network: truncated, corrupted, or compressed differently than `compress` declared. A `ModuleNotFoundError` in `<detail>` means the block references a package the server lacks; an `AttributeError`/`UnpicklingError` on a class means mismatched nnsight versions | Not fatal. Re-send once; if it repeats, compare versions |
| `The model architecture on this server doesn't match the one your code was traced against: it has no module at '<path>'.` followed by the `ndif.compare()` recipe | the same sentence | User's environment: the client built a different module tree, nearly always a `transformers` difference. `<path>` is the *first* diverging path, not the layer the user reached for | Not fatal. Align versions, then retry |
| `Your job exceeded the execution timeout of {n}s.` | `model execution timed out`, `error_type=timeout` | Usually user. Only emitted when `NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` or a per-model `execution_timeout_seconds` is set — there is no default | Not fatal. The in-process interrupt lands at a bytecode boundary and cannot preempt a running CUDA kernel; the sandboxed path kills the runner process |
| `Your job was cancelled or preempted by the server.` | `model execution cancelled`, `error_type=cancelled` | Operator: the actor's `cancel()` fired for a reason other than a HOT→WARM demotion (a demotion re-queues instead) (`src/ndif/services/ray/deployments/modeling/base.py` `BaseModelDeployment.run`) | Re-submit |
| `Request cancelled by operator.` | the kill reply in the CLI | Operator: `ndif kill`. A queued request is removed. An executing one is errored and its replica's worker is cancelled; the Ray call into the actor is not, so the block keeps running there until it finishes (`src/ndif/services/api/queue/dispatcher.py` `Dispatcher._kill`) | Re-submit |
| `Replica was evicted while processing your request. Sorry for the inconvenience. Please try again later.` | `request errored: cancelled mid-dispatch`, `error_type=cancelled` (`ndif.queue.replica`) | Server/operator: the replica's worker task was cancelled mid-dispatch. Because it is sent after any more specific message, it is also the last status a non-blocking job records after a kill or a purge | Retry |
| `Error submitting request to model deployment. Please try again later. Sorry for the inconvenience.` | `request errored during execution` (`ndif.queue.replica`) with the traceback; `error_type` is the Ray-side exception class | Server: the dispatch to the actor failed with something that is not an eviction. If the class is `ActorUnavailableError` the actor is restarting after a fatal CUDA error and the worker waits for it | Retry |
| `Error provisioning model. Please try again later. Sorry for the inconvenience.` / `Error starting model. …` | the same text (`ndif.queue.processor`) with the traceback | Server: the controller could not place or ready a replica and did not say why. The failing phase picks the wording. Every queued request for that model gets it at once (`src/ndif/services/api/queue/processor.py` `Processor.start`, `Processor.purge`) | Retry later |
| `Could not deploy this model. <reason>` | the same, with the controller's text | Usually the caller: a mistyped `repo_id`, a gated repo, or `CANT_ACCOMMODATE` on a full cluster. Only a controller `DeploymentError` is forwarded this way, truncated to `_MAX_REASON_CHARS` | Not until the reason is fixed — see [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) for capacity |
| `Critical server error occurred. Please try again later. Sorry for the inconvenience.` | the dispatcher's error handler, after a Ray connection error | Server: the dispatcher purged every model's queue and is reconnecting (`Dispatcher.handle_errors`) | Retry |

### The two fatal CUDA errors

`format_error` flags two messages as fatal (`src/ndif/services/ray/deployments/modeling/base.py`
`_UNRECOVERABLE_CUDA_ERRORS`): `device-side assert triggered` and
`an illegal memory access was encountered`. They poison the process's CUDA context,
so `run()` restarts the actor (`BaseModelDeployment.restart`) and Ray brings it back
with a reload. The triggering user still gets their traceback; every other user of
that model waits out a load cycle.

## No error text at all

| What happens | What the server logs say | Meaning |
|---|---|---|
| The status shows `QUEUED` a second time | `Replica … evicted (<class>) — re-queueing request, dropping from pool.`, `error_type=evicted:<class>` | An eviction — `CachedActorError` (demoted to WARM, before or during the run), `ActorDiedError`, or a `ValueError` from the actor lookup (`src/ndif/services/api/queue/replica.py` `EVICTED_ERRORS`). The request goes back to the front of its queue and re-runs from the start; a demotion mid-run also logs `model execution preempted; requeued` |
| Sits on `RECEIVED`, `QUEUED`, `PROVISIONING` or `DEPLOYING` | — | [Debug a stuck request](../runbooks/debug-a-stuck-request.md) |
| `COMPLETED` with no saves, no error | — | Early stop (`tracer.stop()`) is normal; otherwise a client-side `.save()` question for nnsight |
| A warning that a `tracer.iter` loop outran the model | — | Not an error: the iterations that were reached are kept |

An exception raised inside an actor reaches the dispatcher wrapped in a
`RayTaskError` whose `isinstance` checks fail over Ray Client; the eviction check
reads `.cause` (`src/ndif/services/api/queue/replica.py` `Replica.dispatch`).

## After `COMPLETED`: download failures

The job succeeded. `data` is either the result itself or, above the inline cap and
for every non-blocking job, a presigned URL the client fetches from the object
store ([Status and Results](../concepts/status-and-results.md)).

| Text the user sees | Whose fault | Fix |
|---|---|---|
| `httpx.ConnectError: [Errno -2] Name or service not known` (or a hang) | Operator | The URL was signed for a host the client cannot reach. `NDIF_OBJECT_STORE_PUBLIC_URL` must be the address *the client* resolves; empty, it falls back to `NDIF_OBJECT_STORE_URL` (`src/ndif/common/providers/objectstore.py` `ObjectStoreProvider`). See [Production](../operating/production.md#3-real-object-storage-reachable-from-two-networks) |
| `403 SignatureDoesNotMatch` | Operator | Signed with different credentials than the store accepts, or a proxy rewrote the Host header |
| `403 Request has expired` | User | Presigned URLs live one hour (`ObjectStoreProvider.presigned_get`); a non-blocking job polled later gets a stale URL. Re-run |
| Download works, `torch.load` fails | Client | A compression mismatch: the client decompresses only if it set `compress` |

## The websocket closes mid-run

| Signature | What happened to the job |
|---|---|
| Closed abnormally, no `ERROR` first | The API restarted. The dispatcher restarts with it and every queued and in-flight request was a Python object in that process — **the job is gone**, with no further status. Re-submit |
| Closed after a long silent stretch | An idle proxy or load balancer timed out. The job is still running; nothing replays it |
| `KeyboardInterrupt` | The client stopped listening; the server keeps going |

A blocking job stores nothing to poll afterwards. Submit with
`remote=True, blocking=False` to survive a disconnect: each non-`LOG` status is
written to `responses/{id}.json` and `GET /response/{id}` reads the latest. A 404
there means "nothing recorded yet", and the client treats it as still running.

## Exceptions only the logs show

| Exception | Raised by | Meaning |
|---|---|---|
| `RunnerError` | `src/ndif/services/ray/sandbox/driver.py` `SandboxDriver.next_event` | The user's block raised in the runner. Carries the runner-formatted traceback text and `cause_type`; `SandboxHost.format_error` returns it verbatim, non-fatal. Always user code |
| `CachedActorError` | `src/ndif/services/ray/deployments/modeling/base.py` `BaseModelDeployment.run` | The actor is WARM, or was demoted mid-run. Raised outside the user-error path on purpose so the queue re-queues |
| `asyncio.CancelledError` | `src/ndif/services/api/queue/replica.py` `Replica.dispatch` | A worker task cancelled by `ndif kill` or a purge; answered with the eviction sentence above, then re-raised |
| `ConnectionError: connection closed before message complete` | `src/ndif/services/ray/sandbox/protocol.py` `recvn` | The runner died, or was stopped. Expected during a timeout or cancel (`SandboxModelDeployment.interrupt` stops it to unblock the host); a real crash otherwise |
| `RuntimeError: runner process exited before its socket was ready` | `src/ndif/services/ray/sandbox/host.py` `spawn` | The runner crashed at import. Its stderr is already in the actor's log |
| `TimeoutError: timed out waiting for the runner socket` | `spawn` | 30 s without the socket accepting — a very slow import or a full `/tmp` |
| `torch.cuda.OutOfMemoryError` at load | `BaseModelDeployment.load_from_disk` | Sizing: the estimate was too small, or the GPU was not actually free. Reaches users as `Error starting model.` — [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) |
| `RuntimeError: '<name>' is still on the meta device …` / `… is on 'cpu', expected one of CUDA devices […]` / `… is on cuda:N, outside the assigned set […]` / `No tensors were placed on assigned GPUs […]` | `src/ndif/services/ray/deployments/modeling/util.py` `verify_device_placement` | The load did not land on the granted GPUs; `cpu` means the budget was too small and accelerate offloaded the overflow |
| An HF error about `trust_remote_code` | the evaluator or the load | The deployment is untrusted and the repo ships custom modelling code; redeploy trusted |
| `NDIFConnectivityError: Cannot connect to Ray at <url>: …` | `src/ndif/cli/lib/_common.py` `ensure_ray_connected` | CLI and dashboard only. Ray is down, the address isn't listening, or the `Controller` actor is missing from the `NDIF` namespace (`RayProvider.connected`) |

An OOM *during* a request is a different case: the weights fit and the
activations didn't. The user gets a CUDA traceback, the replica stays healthy, and
the fix is their batch, sequence length or saved activations — or more padding.

The dispatcher's error queue makes one decision: a connection-level failure
(`src/ndif/common/providers/ray.py` `RayProvider.is_connection_error`, or
`RayProvider.connected()` turning false) purges every queue and reconnects.
Everything else is logged and dropped, because the replica already told the user —
an isolated `request errored during execution` needs no action.
