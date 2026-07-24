---
title: Glossary
one_liner: Alphabetical index of NDIF-specific terms, plus the nnsight terms an NDIF reader needs.
tags: [reference, dev]
related: [docs/concepts/request-lifecycle.md, docs/concepts/deployments-and-eviction.md, docs/concepts/queue-and-scheduling.md, docs/concepts/sandbox-execution.md, docs/developing/controller-internals.md, docs/developing/queue-internals.md, docs/developing/sandbox-internals.md, docs/developing/providers.md, docs/reference/env-vars.md, docs/reference/ports.md, docs/reference/schemas.md]
sources: [src/ndif/common/schema/request.py, src/ndif/common/schema/controller.py, src/ndif/common/types.py, src/ndif/common/providers/base.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/sandbox/ARCHITECTURE.md, src/ndif/cli/service.py]
---

# Glossary

## What this covers

Short definitions of the terms you'll meet in NDIF's code, logs, and docs, in
alphabetical order, each pointing at the page that goes deeper. The last group
covers **nnsight** terms — NDIF is the server for the nnsight client, so its
vocabulary leaks in constantly; those entries define the term and defer to
nnsight's own documentation ([nnsight.net](https://nnsight.net) and the nnsight
repository), which may not be checked out alongside this repo. For environment
variables see [env-vars.md](./env-vars.md); for ports, [ports.md](./ports.md);
for the wire types, [schemas.md](./schemas.md).

## Cluster / Node

A **Node** is one Ray node with GPUs, modelled by the controller as its GPU and
CPU resources plus two maps: its HOT deployments and its WARM cache
(`cluster/node.py:108`). The **Cluster** is the in-memory model of all of them and
the placement/eviction decisions over them (`cluster/cluster.py:25`). Nodes
advertise `cuda_memory_bytes` / `cpu_memory_bytes` as custom Ray resources,
computed at boot by `services/ray/resources.py`. See
[controller-internals.md](../developing/controller-internals.md).

## Controller

The single detached Ray actor named `Controller` in the `NDIF` namespace. It
reconciles desired deployments against live Ray actors through a `build()`/`apply()`
diff and exposes the deploy / evict / get_deployment / status / env API that the
queue, CLI, and dashboard all call. It's launched by `services/ray/start.sh` on the
head node only. See [controller-internals.md](../developing/controller-internals.md).

## Deployment

One **replica** of one model: its placement bookkeeping (which node, which GPUs,
estimated size, pinned or not) plus the Ray actor operations the controller drives
it through — create, delete, cache, from_cache (`cluster/deployment.py:49`).

> **Not a Ray Serve deployment.** NDIF does not use Ray Serve. Each replica is a
> plain **detached Ray actor** created with
> `actor_class.options(lifetime="detached", namespace="NDIF", ...)`
> (`cluster/deployment.py:192`) and looked up by name with `ray.get_actor`. Any
> mention of "Ray Serve deployments" is wrong. See
> [deployments-and-eviction.md](../concepts/deployments-and-eviction.md).

## Deployment level (HOT / WARM / COLD)

The three states a deployment can be in (`cluster/deployment.py:43`). **HOT** —
weights on GPU, serving requests. **WARM** — the actor process is alive but its
weights have been moved to CPU RAM, so it can be restored quickly without a
re-download or re-load from disk; it cannot serve, and a dispatch to it raises
`CachedActorError`. **COLD** — not resident at all. Only HOT replicas are returned
by `get_deployment` (`controller.py:351`), so a model that vanishes from `ndif
status` may still be WARM.

## Dispatcher

The single process that pops requests from the Redis queue and routes each to the
**Processor** for its model key (`api/queue/dispatcher.py:52`). It is not its own
container — the gunicorn master spawns it (`api/gunicorn_conf.py:61`). It is also
the only holder of a Ray client connection on the API side, which is why `/status`
and `/env` go through Redis caches rather than direct RPCs. See
[queue-internals.md](../developing/queue-internals.md).

## Evaluator

`ModelEvaluator` (`cluster/evaluator.py:37`) — estimates a model's GPU footprint by
loading it on the meta device (structure, no weights) via nnsight, counting
parameters and buffers, and padding for runtime overhead
(`NDIF_DEFAULT_PADDING_FACTOR`, `NDIF_DEFAULT_PADDING_BIAS`). The padded byte size
drives placement. Memoized per model key; re-evaluated when dtype or
`trust_remote_code` changes.

## Eviction

Removing a replica to free GPU memory — either explicitly (`ndif evict`, the
dashboard, the reconcile cron) or implicitly, when the cluster needs room for a
new placement. A **pinned** deployment is never evicted, and a deployment younger
than `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default 3600) is protected from
implicit eviction (`node.py:314`, `evictable`). When a replica is evicted out from
under an in-flight request, the queue hands that request back to the *front* of
its model's queue rather than erroring it (`queue/replica.py:12`).

## Extra

A `[project.optional-dependencies]` group in `pyproject.toml` — `api`, `ray`,
`metrics`, `postgres`, `dashboard`, `dev`. Each gates an optional subsystem whose
code degrades cleanly when the packages are absent; installed with
`pip install ".[metrics]"`. See [adding-a-provider.md](../developing/adding-a-provider.md).

## Job

Informal, user-facing synonym for a **request**, used in the status messages the
client sees ("Your job has started running", "Your job exceeded the execution
timeout") and in the non-blocking client API, where `backend.job_id` is the
request `id`. There is no separate job object in the server. Not to be confused
with the dashboard's `jobs/` directory, which holds cron entry points.

## Model actor

The Ray actor class serving one replica, named
`{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace
(`cluster/deployment.py:105`). Two ship in-tree: `ModelActor`
(`modeling/base.py:565`), which runs everything in-process, and
`SandboxModelActor` (`sandbox/model.py:387`), which routes untrusted requests to a
runner. Selected per deployment by `DeploymentConfig.actor_class`, defaulting to
`NDIF_MODEL_IMPORT_PATH` (which itself falls back to `NDIF_DEFAULT_MODEL_ACTOR_CLASS`,
then the base `ModelActor`). See
[model-actor.md](../developing/model-actor.md).

## Model cache

The WARM tier: model weights held in a node's **CPU RAM** so a replica can go back
to HOT without reloading from disk. Its budget is the node's total CPU memory
scaled by `NDIF_MODEL_CACHE_PERCENTAGE` (default `0.9`, `cluster.py:106`) — a *CPU
RAM* fraction, not a GPU one.

## Model key

The canonical string identifying a model: an nnsight wrapper class import path, a
colon, and the wrapper's serialized construction arguments — e.g.
`nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", ...}`.
Produced client-side by `model.to_model_key()` and server-side by
`cli/lib/models.py:16`; it is the queue's partition key, the Redis queue entry's
routing field, a Loki stream label, and an InfluxDB tag. Typed as `MODEL_KEY`
(`common/types.py:4`).

## Park

What a worker does when it needs a value the model hasn't produced yet: it stops
and registers interest in a **location** (`model.transformer.h.0.output`). In the
sandbox, a park crosses the socket as a `PARK` message and is re-tagged and matched
on the host by a `MediatorProxy`; `PARK(id, None)` means the worker finished. See
`src/ndif/services/ray/sandbox/ARCHITECTURE.md` and
[sandbox-internals.md](../developing/sandbox-internals.md).

## Pinning

`DeploymentConfig.pinned` — marks a deployment exempt from autoscaling and
eviction (`common/schema/controller.py:30`). Set by `ndif deploy --pinned`, by
`models.yaml`, and unconditionally by every dashboard schedule entry. Pinning says
nothing about syncing or replica count; it only means "don't take this away".

## Presigned result blob

Results are too large for the Redis response channel, so the model actor uploads
them to the S3-compatible object store and returns a **presigned GET URL** on the
`COMPLETED` response; the client downloads directly. Because a presigned URL is an
HMAC over the request *including the host*, it must be signed with the address the
client will actually hit — hence the two endpoints `NDIF_OBJECT_STORE_URL`
(server-side, e.g. `minio:9000`) and `NDIF_OBJECT_STORE_PUBLIC_URL` (client-facing,
e.g. `localhost:9000`); `providers/objectstore.py:9`. See
[status-and-results.md](../concepts/status-and-results.md).

## Processor

One per model key, owned by the dispatcher: a request queue, a pool of
**Replica** workers sharing it, and an autoscaling loop
(`api/queue/processor.py:57`). Lazy and self-healing — it provisions a replica on
first request, re-provisions when one dies with work still queued, and adds
replicas under sustained queue pressure. It is never torn down; an idle Processor
just sits with an empty pool.

## Provider

NDIF's uniform wrapper around an external service (Redis, Ray, object store,
Postgres, Loki, InfluxDB), living in `src/ndif/common/providers/`. Providers are
classmethod singletons — one connection per process, held on the class, configured
from `NDIF_*` env vars declared in a `CONFIG` dict, established at module import
(`providers/base.py:26`). See [providers.md](../developing/providers.md) and
[adding-a-provider.md](../developing/adding-a-provider.md).

## Queue

The Redis list every accepted request is pushed onto by the API and popped from by
the dispatcher. Its key is `NDIF_QUEUE_KEY` (default `queue`,
`api/queue/config.py:58`). Entries are pickled `BackendRequestModel`s. Per-model
queues exist too, but only in the dispatcher's memory, one per Processor — Redis
holds a single shared list. See [queue-and-scheduling.md](../concepts/queue-and-scheduling.md).

## Replica

One serving instance of a model. Two views of the same thing: on the Ray side a
**Deployment** with a `REPLICA_ID` and a model actor; on the API side a `Replica`
(`api/queue/replica.py:55`), which wraps that actor and owns the asyncio task
pulling from the Processor's queue and dispatching to it. `DeploymentConfig.replicas`
is **additive** — every deploy places that many *new* replicas.

## Request

The unit of work. `BackendRequestModel` (`common/schema/request.py:24`) subclasses
nnsight's `RequestModel` and adds the server-side fields: a fresh uuid `id`, the
`api_key` and resolved `email`, the `trusted` and `priority` flags, the serialized
`payload`, and status-timing state that rides along across the Ray boundary.
Distinct from `session_id`, which addresses the client's websocket. See
[request-lifecycle.md](../concepts/request-lifecycle.md).

## Runner

The separate OS process that executes an untrusted request's traced block, started
as `python -m ndif.services.ray.sandbox.runner <socket>`. A **fresh runner per
request**, stopped afterward, so nothing leaks between requests; `Pool`
(`sandbox/host.py:112`) keeps a couple pre-warmed so acquiring one doesn't pay
Python/nnsight startup. It holds no model; it drives the host's model over the socket.

## Sandbox

The process-based execution split in `src/ndif/services/ray/sandbox/`: the traced
block runs in a **runner** process while the model stays on the host GPU, the two
interleaved over a Unix socket. Isolation is process-based, not VM-based, and today
the runner is an ordinary OS process with no hardening — no namespaces, seccomp,
rlimits, or filesystem jail. The value right now is the *seam*: user code executes
somewhere other than the model actor, behind a narrow protocol. See
`sandbox/ARCHITECTURE.md` and [sandbox-execution.md](../concepts/sandbox-execution.md).

## Service

One of the long-running processes NDIF starts: `api`, `ray`, `dashboard`, plus the
external `redis` and `minio` the CLI can spawn. A service is a name in
`src/ndif/cli/service.py` plus a `start.sh`; one Docker image runs all of them and
`NDIF_SERVICE` picks which — the same value that becomes the `service` label on
every log line and metric point. See [adding-a-service.md](../developing/adding-a-service.md).

## Trusted / untrusted

The most consequential flag in the system. `BackendRequestModel.trusted`
(`common/schema/request.py:57`, default `False`) decides how a request executes:
**trusted** runs the traced block in-process in the model actor, next to the
weights; **untrusted** ships it to a fresh runner and interleaves over a socket
(`sandbox/model.py:242`). It is stamped at ingress from the API key's `trusted`
user_tag — and, when auth is off (`NDIF_POSTGRES_URL` unset), defaulted to `True`
but honoring a client-supplied value, so a caller can send `trusted: false`
(`services/api/auth.py:184`). The same flag rides `DeploymentConfig.trusted` into
`trust_remote_code=` at model load.

> **For self-hosters:** no Postgres configured ⇒ no auth ⇒ a caller's arbitrary
> Python runs in-process next to the weights by default (unless the caller sends
> `trusted: false`), and models load with `trust_remote_code`. See
> [auth-and-limits.md](../concepts/auth-and-limits.md), and
> [testing.md](../developing/testing.md) for exercising the untrusted path locally.

---

## nnsight terms

These belong to the client library. Definitions here are the minimum an NDIF
reader needs; the authority is nnsight's own docs at
[nnsight.net](https://nnsight.net) and the nnsight repository.

### Greenlet worker

A cooperative coroutine (the `greenlet` package) running one block of intervention
code. nnsight interleaves user code and the forward pass with greenlets, not
threads: only one runs at a time, so there are no locks. In NDIF's sandbox the
workers stay in the runner process while their parents move to the host.

### Host (sandbox sense)

NDIF-specific, but easily confused with the nnsight terms: in
`src/ndif/services/ray/sandbox/`, the **host** is the model actor process — the one
holding the weights on GPU and the real PyTorch hooks — as opposed to the
**runner** process holding the user's code. Nothing to do with a network host.

### Interleaver

nnsight's model-side driver. It installs forward pre/post hooks on every module and,
as the forward reaches each location, offers the value to every parked worker and
returns the possibly-edited value back into the run. NDIF's sandbox splits it across
a socket: hooks and the model stay on the host, workers stay in the runner.

### Mediator

The per-block half of the interleaver: it owns one worker greenlet, the occurrence
counter for locations that get revisited, the `tracer.iter` pin, and the read/swap
matching. One mediator per traced block.

### MediatorProxy

NDIF-specific: the host-side half of a split mediator (`sandbox/model.py`). It
**subclasses nnsight's `Mediator` and reuses its `handle` unchanged** — the
matching, iteration, and relaxation logic is the real nnsight code — overriding
only the two methods that touch the boundary: `adopt` (re-tag an untagged park from
the runner) and `switch` (turn a greenlet hop into a `RESUME` → `PARK` round-trip).
One proxy per worker, because occurrence counting and batch slicing are per-worker.

### save

`value.save()` or `nnsight.save(value)` — marks a value inside a traced block to be
returned to the caller. Server-side, saves are what gets collected at the end of a
run, `torch.save`d with a CPU-relocating pickler, and uploaded as the result blob.
The method form is mounted onto `object` by an optional C extension, which is why
`docker/Dockerfile:29` installs `gcc` and `libc6-dev`: without a compiler the
extension is silently skipped and `value.save()` breaks on the server.

### Traced block

The body of a `with model.trace(...):` statement — the arbitrary user Python that
reads and edits activations. nnsight captures and compiles it instead of running
it inline, serializes it, and NDIF runs it server-side. This is the untrusted code
the sandbox exists for.

### Tracer

The object yielded by `model.trace(...)` / `model.session(...)`. It owns the
captured block, the invokes, `tracer.iter`, `tracer.stop()`, `tracer.cache()`, and
`tracer.result`. On the server it is what gets deserialized from the request
payload and executed — in the model actor for a trusted request, in the runner for
an untrusted one.

## Related

- [request-lifecycle.md](../concepts/request-lifecycle.md) — most of these terms in the order a request meets them.
- [deployments-and-eviction.md](../concepts/deployments-and-eviction.md) — deployment, level, replica, pinning, eviction in context.
- [sandbox-internals.md](../developing/sandbox-internals.md) — runner, host, park, MediatorProxy in detail.
- [env-vars.md](./env-vars.md), [ports.md](./ports.md) — the lookup tables this page defers to.
