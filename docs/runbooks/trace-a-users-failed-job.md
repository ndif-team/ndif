---
title: Trace a User's Failed Job
one_liner: From "my trace errored at 14:20" to root cause — correlating one request id across the API logs, the actor logs, Loki and Influx, and telling user error apart from server failure.
tags: [runbook, operating, telemetry, errors, api, sandbox]
related: [docs/errors/client-side-failures.md, docs/errors/server-exceptions.md, docs/concepts/status-and-results.md, docs/concepts/request-lifecycle.md, docs/operating/observability.md, docs/developing/telemetry-internals.md, docs/developing/sandbox-internals.md, docs/runbooks/debug-a-stuck-request.md, docs/runbooks/enable-auth.md]
sources: [src/ndif/common/schema/request.py, src/ndif/common/telemetry.py, src/ndif/common/metrics.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/objectstore.py, src/ndif/services/api/app.py, src/ndif/services/api/queue/replica.py, src/ndif/services/api/queue/processor.py, src/ndif/services/ray/deployments/modeling/base.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/sandbox/nns.py, docker/grafana/provisioning/datasources/loki.yml]
---

# Trace a User's Failed Job

## What this covers

Someone reports a failure with a timestamp and maybe a traceback. This is how to
find the job, decide whether it was their code or your server, and answer them.

One asymmetry governs what evidence still exists, and it's worth knowing before
you start:

| | Blocking request (default) | Non-blocking (`blocking=False`) |
|---|---|---|
| How statuses reach the client | published to a Redis pub/sub channel named by `session_id`, forwarded over the `/subscribe` websocket | written to the object store at `responses/{id}.json` |
| What survives afterwards | **nothing** — pub/sub is fire-and-forget | the **latest** response only, overwritten each time |
| Where the user's `print()` goes | `Status.LOG` responses, streamed live | dropped (`respond` skips LOG when there's no session) |

(`common/schema/request.py:134-159`.) So for a blocking job — which is what
almost everyone runs — the only durable record is the logs and metrics. Get the
user's console output if you can.

## Step 1 — find the request id

Best case, the user has it: the client's status line prints it dimmed in brackets
before every status, e.g. `[3f9c1e2b…] ✗ ERROR`.

Otherwise, search by identity and time. With auth on, `email` is resolved at
ingress and rides with the request into every downstream log record
(`common/schema/request.py:44-51`), so in Grafana Explore over a window around
14:20:

```logql
{environment=~".+"} | json | email=`researcher@example.edu` | stage=`error`
```

Without auth there is no email — fall back to the model and the window:

```logql
{environment=~".+", model_key=~".*Llama-3.1-8B.*"} | json | stage=`error`
```

`model_key` is a Loki **stream label** (promoted per record,
`providers/loki.py:47-50`); `email`, `request_id`, `api_key`, `stage` and the
timings live in the JSON line and need `| json`. Once you have an id, one query
gives you the whole job across every service:

```logql
{environment=~".+"} | json | request_id=`3f9c1e2b...`
```

That's the same query the provisioned Loki datasource wires onto every
`request_id` it finds in a log line, as a clickable "All logs for this request"
link (`docker/grafana/provisioning/datasources/loki.yml`).

Without Loki (`NDIF_LOKI_URL` unset), the identical records go to each process's
console: `just logs api` for the API and the dispatcher, and the Ray dashboard's
per-actor log view for the model actor.

## Step 2 — read the lifecycle timeline

`BackendRequestModel._advance_status` logs exactly one record per status
transition under the `ndif.request` logger, carrying `stage`, `prev_stage`, and
`prev_stage_ms` (`common/schema/request.py:121-132`). Sorted by time, that is the
job's timeline:

```logql
{environment=~".+", logger="ndif.request"} | json | request_id=`3f9c1e2b...`
```

```
stage=received                     prev_stage=null
stage=queued    prev_stage=received  prev_stage_ms=4.1
stage=dispatched prev_stage=queued   prev_stage_ms=812.6
stage=running   prev_stage=dispatched prev_stage_ms=6.2
stage=error     prev_stage=running   prev_stage_ms=48213.9
```

The **last stage reached** localizes the failure:

| Last stage | Who owns the failure | Next |
|---|---|---|
| nothing at all | rejected at ingress before a request object existed — look for `api request rejected` under `ndif.api` (`api/app.py:64-85`): 401/400/403 auth, 422 bad envelope, 400 version gate, 503 backend down | [enable-auth](enable-auth.md), [http-api](../reference/http-api.md) |
| `received` | never enqueued — the API errored between accepting and `lpush` | `ndif.api`, `api unhandled error` |
| `queued` / `provisioning` / `deploying` | provisioning failed; the user got a canned "Error starting/provisioning model" | `ndif.queue.processor` |
| `dispatched` | the handoff to the actor failed, or the replica was evicted mid-flight | `ndif.queue.replica` |
| `running` | execution failed — the interesting case | Step 3 |

## Step 3 — user code, or server failure?

**The rule: if the user's error text is a Python traceback, it's their code. If
it's one of a handful of fixed English sentences, it's ours.**

That holds because of how the error text is produced. On the sandboxed
(untrusted) path the runner catches the exception *where it is live*, formats the
traceback there — tracebacks don't survive cloudpickle — and ships the text back
as an `EXCEPTION` event:

```python
        tb = getattr(error, "__intervention_tb__", None) or clean_traceback(
            error.__traceback__
        )
        message = "".join(traceback.format_exception(type(error), error, tb))
        terminal = ("EXCEPTION", message)
```

— `src/ndif/services/ray/sandbox/nns.py:514-522`. The host turns that event into
a `RunnerError` carrying the text (`sandbox/model.py:228-229`), and
`SandboxModelDeployment.format_error` returns it verbatim and marks it
**non-fatal — it's user code** (`sandbox/model.py:188-194`). `run()` then sends it
as the `Status.ERROR` description (`modeling/base.py:344-346`), and nnsight's
`RemoteBackend.note` raises `RemoteError(response.description)`
(`nnsight/intervention/backends/remote.py:161-162`). The user's screen shows a
traceback of *their own* code, produced on the server.

On the trusted (in-process) path the same thing happens with the base
implementation: `clean_traceback` strips nnsight's plumbing and `filter_traceback`
drops the actor's own frames, leaving the user's block
(`modeling/base.py:444-454`).

Server-side failures never carry a traceback. Both deserialization failures
below are classified in one place — `BackendRequestModel.deserialize` — which
the in-process actor and the sandbox runner both call, so the two paths cannot
drift. The complete set of canned descriptions a user can receive:

| Message the user sees | Emitted at | What actually happened |
|---|---|---|
| `Your job exceeded the execution timeout of Ns.` | `modeling/base.py:322-326` | ran past `execution_timeout` |
| `Your job was cancelled or preempted by the server.` | `modeling/base.py`, the `kill in done` branch | the actor's kill switch fired for a reason other than parking — somebody deliberately cancelled this request. A HOT→WARM demotion does **not** land here: `to_cache` passes `KILL_REASON_PREEMPTED`, which raises `CachedActorError` and re-queues. If you are chasing a *disappeared* request rather than a failed one, grep for `model execution preempted; requeued`. |
| `Replica was evicted while processing your request.` | `queue/replica.py:211-215`, `:289-293` | the worker task was cancelled mid-dispatch. Reached only by `ndif kill` and `purge`; an ordinary eviction re-queues the request rather than erroring it, so this message means somebody or something deliberately tore the replica down. |
| `Your request payload could not be read (...)` | `common/errors.py:PayloadError` | the blob failed to deserialize — truncated, corrupted, or a compress-flag mismatch. **Precedes any user code**, so despite being the caller's problem it is deliberately *not* a traceback |
| `The model architecture on this server doesn't match ...` | `common/errors.py:ArchitectureMismatchError` | a `Module:<path>` the server's tree doesn't have — the client and server built different trees, nearly always a `transformers` version difference. Names the *first* diverging path, not the layer the user asked for, because the block references the whole envoy tree. Points the user at `ndif.compare()` |
| `Request cancelled by operator.` | `queue/dispatcher.py:346` | someone ran `ndif kill` |
| `Error submitting request to model deployment.` | `queue/replica.py:254-258` | the Ray call to the actor raised something unclassified |
| `Error starting model.` / `Error provisioning model.` | `queue/processor.py:179`, `:194` | the controller couldn't place or ready a replica, **and did not say why** — an internal fault; the traceback is in the API log |
| `Could not deploy this model. <reason>` | `queue/processor.py`, the `DeploymentError` branch of `start` | the controller refused and explained: mistyped `repo_id`, gated repo, `CANT_ACCOMMODATE`. The reason is the controller's own text, forwarded verbatim |
| `Critical server error occurred.` | `queue/dispatcher.py:195`, `processor.py:374-378` | a Ray connection error purged every Processor |

Each of those also produces a structured server log with the real cause. The
mapping:

```logql
{environment=~".+", severity=~"error|warning"} | json | request_id=`3f9c1e2b...`
```

| Log message | Logger | `error_type` |
|---|---|---|
| `model execution errored` | `ndif.modeling` | the exception class — `RunnerError` for user code |
| `model execution timed out` | `ndif.modeling` | `timeout`, with `execution_timeout` |
| `model execution cancelled` | `ndif.modeling` | `cancelled` |
| `request errored during execution` | `ndif.queue.replica` | the Ray-side exception class, with `exc_info` |
| `request errored: cancelled mid-dispatch` | `ndif.queue.replica` | `cancelled` |
| `Replica … evicted (…) — re-queueing request` | `ndif.queue.replica` | `evicted:<class>` |

`ndif.modeling` records come from the model actor, whose Loki `service` label is
**`model`**, not `ray` — the controller overrides `NDIF_SERVICE` in each actor's
`runtime_env` (`cluster/deployment.py:184-187`). Filtering `{service="ray"}` will
miss them.

> **Not an error, but it looks like one in the logs:** user `print()` output
> arrives as repeated non-terminal `Status.LOG` responses — `PRINT` events on the
> sandboxed path (`sandbox/model.py:222-227`), a `LogStream` stdout redirect on
> the trusted one (`modeling/util.py:16-41`). LOG never advances the lifecycle
> (`request.py:99`) and is never persisted for a non-blocking job.

## Step 4 — where the result would have gone

Two different objects, same bucket (`NDIF_OBJECT_STORE_BUCKET`, default
`ndif-results`):

| Key | Written by | When |
|---|---|---|
| `{request_id}.pt` | `upload_bytes` (`modeling/base.py:536-561`) | **only on success**, after `execute` returns |
| `responses/{request_id}.json` | `respond`/`arespond` for a request with no `session_id` | every non-LOG status of a non-blocking job, overwritten |

So a failed job has **no `.pt` blob** — execution never got past the exception.
But a failed *non-blocking* job still has its last response on disk, and that
response holds the full error description:

```bash
curl -s localhost:8001/response/3f9c1e2b... | jq .
```

```json
{"id": "3f9c1e2b...", "status": "ERROR", "description": "Traceback (most recent call last):\n  ...", "data": null}
```

`GET /response/{id}` reads that object directly and 404s if it isn't there
(`api/app.py:304-318`). For a blocking job it is always 404 — nothing was ever
written.

On success the actor uploads the `torch.save` blob, optionally zstd-compressed to
match the request, and returns a **presigned GET url valid for one hour**
(`objectstore.py:159-168`), which rides on the COMPLETED response as `data`. The
client downloads it and injects the values.

## Step 5 — "it completed but the user got nothing"

This is almost always the presigned-url misconfiguration, and it is the most
common real one.

The provider keeps **two** S3 clients (`providers/objectstore.py:9-17`):

- `NDIF_OBJECT_STORE_URL` — reached by the *server*; used to upload.
- `NDIF_OBJECT_STORE_PUBLIC_URL` — reached by the *client*; used only to **sign**
  the GET url. Defaults to `url` when unset.

A presigned url is an HMAC over the request including the host, so it must be
signed with the host the downloader will actually hit. Signing with the internal
host produces a url like `http://minio:9000/ndif-results/3f9c….pt?X-Amz-…` which
the user's machine cannot resolve — or, if they rewrite the host, one that fails
signature verification.

Symptoms: the job reaches `COMPLETED` server-side with a clean
`stage=completed` record and a `response_size` metric, and the user reports a
connection error or an S3 `SignatureDoesNotMatch` instead of results.

Check what the server would hand out:

```bash
docker compose -f docker/docker-compose.yml exec api python -c "
from ndif.common.providers.objectstore import ObjectStoreProvider as O
print('upload  :', O.url)
print('presign :', O.public_url or O.url)
print(O.presigned_get('probe.pt'))"
```

The compose default is `NDIF_OBJECT_STORE_URL=http://minio:9000` with
`NDIF_OBJECT_STORE_PUBLIC_URL=http://localhost:9000` (`docker-compose.yml:223-224` for the ray service, `:150-151` for the api)
— correct for a client on the same machine, wrong for anyone else. In a real
deployment `public_url` must be the externally-routable object-store address.

Verify the blob exists at all, and that its size is sane:

```bash
docker compose -f docker/docker-compose.yml exec minio \
  mc ls local/ndif-results/3f9c1e2b....pt
```

## Step 6 — cross-check the metrics

Influx measurements, all tagged `model_key` / `api_key` / `email` plus a metric-
specific dimension, with `request_id` as a *field* (deliberately not a tag, to
bound series cardinality — `common/metrics.py:26-29`):

| Measurement | Tag that matters | Fields |
|---|---|---|
| `status_time` | `status` (the phase being *left*) | `duration_ms`, `request_id` |
| `execution_time` | `status` = `completed`/`error`/`timeout`/`cancelled` | `exec_ms`, `deserialize_ms`, `upload_ms` |
| `request_size` | — | `payload_bytes`, `ip_address`, `user_agent`, `session_id` |
| `response_size` | — | `response_bytes`, `compressed` |
| `gpu_mem` | `gpu_index` | `baseline_bytes`, `peak_bytes`, `extra_bytes` |
| `model_load_time` | `load_type` = `initial`/`from_cache` | `duration_ms`, `num_gpus` |

In Grafana Explore against the InfluxDB datasource (Flux):

```flux
from(bucket: "metrics")
  |> range(start: 2026-07-22T14:00:00Z, stop: 2026-07-22T14:40:00Z)
  |> filter(fn: (r) => r._measurement == "execution_time")
  |> filter(fn: (r) => r.email == "researcher@example.edu")
  |> filter(fn: (r) => r.status == "error")
```

What each tells you about a failure:

- `execution_time` with `status="error"` and a large `exec_ms` — the block ran a
  long time before throwing. With a tiny `exec_ms` it failed immediately, often
  in deserialization (compare `deserialize_ms`).
- `status_time` for `status="queued"` — how long they waited; a huge value with a
  successful run means the user experienced "hung", not "failed".
- `gpu_mem.extra_bytes` near the model's padding budget — an OOM, see
  [model-oom-on-deploy](model-oom-on-deploy.md).
- No metrics at all for the window — Influx is optional and fail-open
  (`NDIF_INFLUX_URL`); absence of metrics is not evidence of absence of requests.

## Step 7 — what to tell the user

| Finding | Message |
|---|---|
| Traceback pointing at their block | Their code. The traceback is real and the line numbers refer to their own source (the block's source is registered in the server's `linecache` so frames resolve). |
| `exceeded the execution timeout of 3600s` | Their trace ran past the cap. Split the work or ask for a longer per-deployment `execution_timeout_seconds`. Note the timeout can't interrupt a native call already in flight, so the real runtime may exceed the number in the message. |
| `Replica was evicted while processing your request.` | Server-side preemption. Retry. If it repeats, the model is being evicted under memory pressure — [model-oom-on-deploy](model-oom-on-deploy.md). |
| `Error starting/provisioning model.` | The cluster couldn't place the model. Not their fault, not retryable until capacity exists. |
| `Critical server error occurred.` | Ray connection loss; everything queued at that moment was errored. Retry. |
| 401 / 403 at ingress | Their key. Point them at key issuance; `/whoami` confirms what the server sees. |
| 400 mentioning a version | The client version gate (`NDIF_MIN_NNSIGHT_VERSION` / `NDIF_MIN_PYTHON_VERSION`). Tell them the minimum, and give them `ndif env` to compare package versions against the cluster. |
| COMPLETED server-side, nothing client-side | Your object-store URL configuration, Step 5. Fix and ask them to re-run. |

## Gotchas

- **A blocking job that disconnects is invisible from then on.** Statuses keep
  being published to a channel nobody is subscribed to; the job still runs to
  completion and still uploads its blob. The user has no way to get it back — the
  presigned url was only ever sent over the socket they dropped.
- **`request_id` is not `session_id`.** `session_id` addresses the websocket and
  is minted by `/subscribe`; `id` is minted per request (`request.py:44`). Users
  quoting "the id from the URL" are quoting the wrong one.
- **`ndif queue` shows only live state.** A finished or failed request is gone
  from the dispatcher's memory immediately. It is the wrong tool for a
  post-mortem; logs and metrics are the record.
- **With auth off there is no `email`**, and `api_key` is whatever the client
  chose to send (it's carried for telemetry even when unverified,
  `auth.py:164-165`). Per-user attribution is a reason to
  [turn auth on](enable-auth.md).

## Related

- [docs/errors/client-side-failures.md](../errors/client-side-failures.md) — what
  each client-visible failure means server-side.
- [docs/concepts/status-and-results.md](../concepts/status-and-results.md) — the
  status lifecycle, response channels, and result blobs.
- [docs/operating/observability.md](../operating/observability.md) — the Grafana
  dashboards and how logs and metrics are wired.
- [docs/runbooks/debug-a-stuck-request.md](debug-a-stuck-request.md) — when the
  job never finished at all.
