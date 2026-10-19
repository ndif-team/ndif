---
title: Trace a User's Failed Job
one_liner: From "my trace errored at 14:20" to root cause — correlating one request id across the API logs, the actor logs, Loki and Influx, and telling user error apart from server failure.
tags: [runbook, operating, telemetry, errors, api, sandbox]
related: [docs/reference/error-messages.md, docs/concepts/status-and-results.md, docs/concepts/request-lifecycle.md, docs/operating/observability.md, docs/operating/troubleshooting.md, docs/developing/telemetry-internals.md, docs/developing/sandbox-internals.md, docs/runbooks/debug-a-stuck-request.md, docs/runbooks/enable-auth.md]
sources: [src/ndif/common/schema/request.py, src/ndif/common/metrics.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/objectstore.py, src/ndif/services/api/app.py, src/ndif/services/api/auth.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/queue/processor.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/deployments/modeling/util.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/sandbox/nns.py, docker/grafana/provisioning/datasources/loki.yml]
---

# Trace a User's Failed Job

## What this covers

Someone reports a failure with a timestamp and maybe a traceback. This is how to
find the job, decide whether it was their code or your server, and answer them.

What evidence survives depends on how the job was submitted
(`src/ndif/common/schema/request.py` `BackendRequestModel.respond`):

| | Blocking (default) | Non-blocking (`blocking=False`) |
|---|---|---|
| Statuses reach the client | over Redis pub/sub on the client's `session_id` channel, forwarded by the `/subscribe` websocket | written to the object store at `responses/{id}.json`, polled with `GET /response/{id}` |
| Afterwards | **nothing** — pub/sub keeps no copy | the **latest** response, overwritten by each later one |
| The block's `print()` output | `LOG` responses, streamed live | dropped |

For a blocking job, which is almost everyone, the only server-side record is the
logs and metrics. Get the user's console output if you can.

## Step 1 — find the request id

The client prints it in brackets before every status: `[3f9c1e2b…] ERROR …`.

Otherwise search by identity and time. With auth on, `email` is resolved at
ingress and rides on every log record, so in Grafana Explore over a window around
the reported time:

```logql
{environment=~".+"} | json | email=`researcher@example.edu` | stage=`error`
```

Without auth there is no email; fall back to the model:

```logql
{environment=~".+", model_key=~".*Llama-3.1-8B.*"} | json | stage=`error`
```

`model_key` is a Loki stream label; `email`, `request_id`, `api_key`, `stage` and
the timings are in the JSON line and need `| json`. With an id, one query returns
the whole job across every service — the same "All logs for this request" link
the provisioned Loki datasource puts on each `request_id`:

```logql
{environment=~".+"} | json | request_id=`3f9c1e2b...`
```

Without Loki (`NDIF_LOKI_URL` unset) the same records are on each process's
console: `just logs api` for the API and dispatcher, the Ray dashboard's
per-actor log for the model actor.

## Step 2 — read the lifecycle timeline

`BackendRequestModel._advance_status` logs one `ndif.request` record per status
transition, with `stage`, `prev_stage` and `prev_stage_ms`:

```logql
{environment=~".+", logger="ndif.request"} | json | request_id=`3f9c1e2b...`
```

```
stage=received
stage=queued      prev_stage=received    prev_stage_ms=4.1
stage=dispatched  prev_stage=queued      prev_stage_ms=812.6
stage=running     prev_stage=dispatched  prev_stage_ms=6.2
stage=error       prev_stage=running     prev_stage_ms=48213.9
```

Only the first `ERROR` is logged; a later one for the same request (see
[Killed jobs](#killed-jobs)) is sent but not logged again.

| Last stage | Where it failed | Look at |
|---|---|---|
| nothing | rejected before a request object existed: 401/400/403 auth, 422 bad envelope, 400 client version gate, 503 backend down — `api request rejected: <code>` | `ndif.api`; [Enable auth](enable-auth.md) |
| `received` | never enqueued — the API failed between accepting and pushing to Redis | `ndif.api`, `api unhandled error` |
| `queued` / `provisioning` / `deploying` | provisioning failed and every queued request for the model was errored | `ndif.queue.processor` |
| `dispatched` | the handoff to the actor failed, or the job was killed or purged before the actor reported running | `ndif.queue.replica` |
| `running` | execution failed | Step 3 |

## Step 3 — user code, or server failure?

**If the user's error text is a Python traceback ending in their block, it's
their code. If it's one of a fixed set of English sentences, or a traceback whose
last frames are greenlet/threading or NDIF paths, it's the server.**

On the untrusted path the runner catches the exception where it is live, formats
the traceback there and ships the text back
(`src/ndif/services/ray/sandbox/nns.py` `_run`);
`SandboxHost.format_error` passes it through as a non-fatal user error
(`src/ndif/services/ray/sandbox/model.py` `SandboxHost.format_error`). On the
trusted path `BaseModelDeployment.format_error`
(`src/ndif/services/ray/deployments/modeling/base.py`) strips only nnsight's
frames and those of its own file. The traceback the user sees still carries
asyncio, Ray and other NDIF wrapper frames, and torch/transformers frames, before
the frames of their own block; read it from the bottom.

Every fixed sentence — what it means, whose fault, whether a retry helps — is in
[Error messages](../reference/error-messages.md). Look up the exact text there.

The server's own record of the failure:

```logql
{environment=~".+", severity=~"error|warning"} | json | request_id=`3f9c1e2b...`
```

`model execution errored` / `timed out` come from the model actor, whose Loki
`service` label is **`model`**, not `ray`; a `{service="ray"}` filter misses them.
`error_type` on those records is the user's exception class on both paths.

### Killed jobs

An operator `ndif kill` of an executing request produces two `ERROR` responses in
a row (`src/ndif/services/api/queue/dispatcher.py` `Dispatcher._kill`,
`src/ndif/services/api/queue/replica.py` `Replica.cancel`): first `Request
cancelled by operator.` from the dispatcher, then `Your job was cancelled or
preempted by the server.` from the actor, which the kill has cancelled
(`BaseModelDeployment.run`). The actor's log shows `model execution cancelled`,
and the replica keeps serving.

- **Blocking job:** the client raises on the first `ERROR`, and its description
  shows empty.
- **Non-blocking job:** `responses/{id}.json` is overwritten by the second, so
  polling shows the actor's sentence.

If the actor does not finish the request within a few seconds of the kill (it
had not started executing yet, or it is wedged), the dispatcher cancels the
dispatch instead: the second response is then `Replica was evicted while
processing your request. …`, the replica's worker exits, and the actor may still
run the block to completion, its own final response (`COMPLETED`, or an `ERROR`)
overwriting a non-blocking job's stored one.

## Step 4 — what the server kept

A failed job has no result blob; the actor only uploads on success. A failed
**non-blocking** job still has its last response, with the full error text:

```bash
curl -s localhost:8001/response/3f9c1e2b... | jq '{status, description, meta}'
```

`GET /response/{id}` 404s when nothing was written, which for a blocking job is
always (`src/ndif/services/api/app.py` `get_response`). `meta` on a terminal
response is what the run cost — `runtime`, per-GPU memory above the weights, and
on an OOM `alloc_shortfall_by_gpu` ([Model OOM on deploy](model-oom-on-deploy.md)).

How a successful result travels — inline on the response, or as a presigned URL
for large and non-blocking results — is in
[Status and results](../concepts/status-and-results.md).

## Step 5 — "it completed but the user got nothing"

Almost always the presigned URL was signed for a host the user can't reach.
Server-side the job looks clean — a `stage=completed` record and a
`response_size` metric — while the user reports a connection error or
`SignatureDoesNotMatch`. Only results above the inline cap and non-blocking jobs
use the URL, which is why small traces work and large ones don't.

Check what the server hands out:

```bash
docker compose -f docker/docker-compose.yml exec api python -c "
from ndif.common.providers.objectstore import ObjectStoreProvider as O
print('upload  :', O.url)
print('presign :', O.public_url or O.url)
print(O.presigned_get('probe.pt'))"
```

The presign host must be the object-store address the user's machine resolves.
Compose's `http://localhost:9000` is right only for a client on the same machine;
set `NDIF_OBJECT_STORE_PUBLIC_URL` on both `api` and `ray`. The mechanism is in
[Status and results](../concepts/status-and-results.md) and the fix in
[Troubleshooting](../operating/troubleshooting.md).

Confirm the blob exists and has a sane size:

```bash
docker compose -f docker/docker-compose.yml exec api python -c \
  "from ndif.common.providers.objectstore import ObjectStoreProvider as S; S.connect(); \
   print(S.client.head_object(Bucket=S.bucket, Key='3f9c1e2b....pt')['ContentLength'])"
```

## Step 6 — cross-check the metrics

Influx measurements (`src/ndif/common/metrics.py`). `request_id` is a field, not a
tag, to bound series cardinality; per-request measurements are tagged
`model_key`, `api_key` and `email`.

| Measurement | Tag that matters | Fields |
|---|---|---|
| `status_time` | `status`, the phase being *left*: `SENT`, `RECEIVED`, `QUEUED`, … | `duration_ms`, `request_id` |
| `execution_time` | `status`: `completed` / `error` / `timeout` / `cancelled` / `preempted` | `exec_ms`, `deserialize_ms`, `upload_ms` |
| `request_size` | — | `payload_bytes`, `ip_address`, `user_agent`, `session_id` |
| `response_size` | — | `response_bytes`, `compressed` |
| `gpu_mem` | `gpu_index` | `baseline_bytes`, `peak_bytes`, `extra_bytes` |
| `model_load_time` | `load_type`: `initial` / `from_cache` | `duration_ms`, `num_gpus` |

In Grafana Explore against the InfluxDB datasource (Flux):

```flux
from(bucket: "metrics")
  |> range(start: 2026-07-22T14:00:00Z, stop: 2026-07-22T14:40:00Z)
  |> filter(fn: (r) => r._measurement == "execution_time")
  |> filter(fn: (r) => r.email == "researcher@example.edu")
  |> filter(fn: (r) => r.status == "error")
```

- `execution_time`, `status="error"`, large `exec_ms`: the block ran a long time
  before throwing. Tiny `exec_ms`: it failed at once, often in deserialization
  (compare `deserialize_ms`).
- `execution_time`, `status="preempted"`: the replica was demoted mid-run and the
  request re-ran; the user saw a second `QUEUED`, not an error.
- `status_time` with `status="QUEUED"`: how long they waited. A huge value with a
  successful run means the user experienced "hung", not "failed".
- `gpu_mem.extra_bytes` near the model's padding: an OOM —
  [Model OOM on deploy](model-oom-on-deploy.md).
- No metrics for the window: Influx is optional and fail-open (`NDIF_INFLUX_URL`).
  Missing metrics are not evidence of missing requests.

## Step 7 — what to tell the user

| Finding | Message |
|---|---|
| Traceback through their block | Their code. The line numbers refer to their own source. |
| `exceeded the execution timeout of Ns` | Their trace ran past the cap. Split the work, or ask for a longer per-deployment `execution_timeout_seconds`. |
| `Request cancelled by operator.`, or the eviction sentence right after a kill | You killed it. Say why. |
| `Replica was evicted while processing your request.` with no kill | Server-side; retry. If it repeats, the model is being evicted under memory pressure — [Model OOM on deploy](model-oom-on-deploy.md). |
| `Could not deploy this model. <reason>` | The controller refused the deploy and said why — a wrong or gated repo, or no room. Not retryable until that changes. |
| `Error starting model.` / `Error provisioning model.` | The cluster couldn't place or start the model and gave no reason. Retry later; if it repeats, check `ndif.queue.processor` and [Model OOM on deploy](model-oom-on-deploy.md). |
| `Critical server error occurred.` | The API lost Ray; everything queued at that moment was errored. Retry. |
| 401 / 403 at ingress | Their key. `/whoami` confirms what the server sees. |
| 400 mentioning a version | The client version gate (`NDIF_MIN_NNSIGHT_VERSION` / `NDIF_MIN_PYTHON_VERSION`). Tell them the minimum; `ndif env` lists the cluster's versions. |
| `COMPLETED` server-side, nothing client-side | Your object-store URL configuration, Step 5. Fix it and ask them to re-run. |

## Gotchas

- **A blocking job that disconnects is invisible from then on.** It still runs to
  completion; its statuses go to a channel nobody reads, and the result — inline
  or a presigned URL — was only ever sent over the dropped socket.
- **`request_id` is not `session_id`.** `session_id` addresses the websocket; `id`
  is minted per request (`src/ndif/common/schema/request.py`
  `BackendRequestModel`). Users quoting "the id from the URL" are quoting the
  wrong one.
- **`ndif queue` shows only live state.** A finished or failed request leaves the
  dispatcher's memory at once; logs and metrics are the record.
- **With auth off there is no `email`**, and `api_key` is whatever the client sent,
  carried unverified for telemetry (`src/ndif/services/api/auth.py`
  `validate_request`). Per-user attribution is a reason to
  [turn auth on](enable-auth.md).

## Related

- [Error messages](../reference/error-messages.md) — every client-visible failure
  by exact text.
- [Status and results](../concepts/status-and-results.md) — the status
  lifecycle, delivery routes and result blobs.
- [Observability](../operating/observability.md) — dashboards and how logs and
  metrics are wired.
- [Debug a stuck request](debug-a-stuck-request.md) — when the job never finished.
