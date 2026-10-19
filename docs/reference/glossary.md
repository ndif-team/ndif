---
title: Glossary
one_liner: Alphabetical index of NDIF-specific terms, plus the nnsight terms an NDIF reader needs.
tags: [reference, dev]
related: [docs/concepts/request-lifecycle.md, docs/concepts/deployments-and-eviction.md, docs/concepts/queue-and-scheduling.md, docs/concepts/sandbox-execution.md, docs/developing/controller-internals.md, docs/developing/queue-internals.md, docs/developing/sandbox-internals.md, docs/developing/providers.md, docs/reference/env-vars.md, docs/reference/ports.md, docs/reference/schemas.md]
sources: [src/ndif/common/schema/request.py, src/ndif/common/schema/controller.py, src/ndif/common/types.py, src/ndif/common/providers/base.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/api/queue/processor.py, src/ndif/services/api/queue/replica.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/sandbox/driver.py, src/ndif/cli/service.py]
---

# Glossary

## What this covers

Short definitions of the terms in NDIF's code, logs and docs, alphabetical, each
pointing at the page that goes deeper. The last group covers the **nnsight** terms
an NDIF reader meets; the authority for those is nnsight's own documentation
([nnsight.net](https://nnsight.net)). Variables are in [env-vars.md](env-vars.md),
ports in [ports.md](ports.md), wire types in [schemas.md](schemas.md).

## Cluster / Node

A **Node** is one Ray node as the controller models it: GPU and CPU resources, its
HOT deployments and its WARM cache
(`src/ndif/services/ray/deployments/controller/cluster/node.py` `Node`). The
**Cluster** is all of them plus the placement and eviction decisions over them
(`.../cluster/cluster.py` `Cluster`). See
[controller-internals.md](../developing/controller-internals.md).

## Controller

The single detached Ray actor named `Controller` in the `NDIF` namespace, started
on the head node by `src/ndif/services/ray/start.sh`. It diffs desired deployments
against live actors (`build()`/`apply()`) and serves the deploy / evict /
get_deployment / status / env calls the queue, CLI and dashboard make. See
[controller-internals.md](../developing/controller-internals.md).

## Deployment

One replica of one model: its placement bookkeeping plus the actor operations the
controller drives it through
(`src/ndif/services/ray/deployments/controller/cluster/deployment.py` `Deployment`).
Not a Ray Serve deployment — NDIF does not use Serve; each replica is a plain
detached Ray actor. See
[deployments-and-eviction.md](../concepts/deployments-and-eviction.md).

## Deployment level (HOT / WARM / COLD)

**HOT**: weights on GPU, serving. **WARM**: actor alive, weights moved to CPU RAM
for a fast restore; a dispatch to it raises `CachedActorError` and the request is
re-queued. **COLD**: not resident (`DeploymentLevel` in `deployment.py`). Only HOT
replicas are returned by `get_deployment`. See
[deployments-and-eviction.md](../concepts/deployments-and-eviction.md).

## Dispatcher

The single process, spawned by the gunicorn master, that pops requests from the
Redis queue and routes each to its model's **Processor**
(`src/ndif/services/api/queue/dispatcher.py` `Dispatcher`). It is the only API-side
holder of a Ray client, which is why `/status` and `/env` go through Redis caches.
See [queue-internals.md](../developing/queue-internals.md).

## Evaluator

`ModelEvaluator` (`src/ndif/services/ray/deployments/controller/cluster/evaluator.py`)
sizes a model for placement: nnsight's `Remotable.describe_checkpoint` reads the
checkpoint's metadata for its size, parameter count, config and revision, and the
evaluator pads that (`NDIF_DEFAULT_PADDING_FACTOR`, `NDIF_DEFAULT_PADDING_BIAS`).
Memoized per model key (which carries `trust_remote_code`); re-evaluated when dtype
changes. See
[controller-internals.md](../developing/controller-internals.md).

## Eviction

Removing a replica to free GPU memory — explicitly (`ndif evict`, the dashboard,
its reconcile cron) or to make room for a placement. A pinned deployment is never
evicted to make room, and one younger than `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS`
only for a pinned placement (`Node.evictable`). A request whose replica is evicted mid-flight is
re-queued at the front, not errored. See
[deployments-and-eviction.md](../concepts/deployments-and-eviction.md).

## Extra

A `[project.optional-dependencies]` group in `pyproject.toml`: `api`, `ray`,
`ext` (libraries user blocks import server-side), `metrics`, `postgres`,
`dashboard`, `dev`. See [configuration.md](../operating/configuration.md).

## Job

User-facing synonym for a **request** ("Your job has started running"); a
non-blocking client's job id is the request `id`. There is no separate job object.
Not the dashboard's `jobs/` directory, which holds its cron entry points.

## Model actor

The Ray actor serving one replica, named `{replica_id}:ModelActor:{model_key}` in
the `NDIF` namespace (`Deployment.name`). In-tree: `ModelActor` (in-process),
`SandboxModelActor` (untrusted requests in a runner), and for tensor parallelism
`TPModelActor` / `SandboxedTPModelActor`. Chosen per deployment by
`DeploymentConfig.actor_class`, else `NDIF_MODEL_IMPORT_PATH`. See
[model-actor.md](../developing/model-actor.md).

## Model cache

The WARM tier: weights in a node's **CPU RAM**, budgeted at total RAM ×
`NDIF_MODEL_CACHE_PERCENTAGE`. See
[deployments-and-eviction.md](../concepts/deployments-and-eviction.md).

## Model key

The string identifying a model: the nnsight wrapper's import path, a colon, and its
JSON identity, e.g.
`nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", "revision": null, "task": "text-generation"}`.
The `task` is part of it, so one repo under two tasks is two models. Minted
client-side by `model.to_model_key()` and by the CLI's `get_model_key`
(`src/ndif/cli/lib/models.py`); the queue's partition key and a telemetry label.
See [queue-and-scheduling.md](../concepts/queue-and-scheduling.md).

## Park

A worker stopping to wait for a value the model hasn't produced yet, registering
interest in a location (`model.transformer.h.0.output`). In the sandbox it crosses
the socket as a `PARK` message. See
[sandbox-internals.md](../developing/sandbox-internals.md).

## Pinning

`DeploymentConfig.pinned`: exempt from autoscaling and eviction. Set by
`ndif deploy --pinned`, `models.yaml`, and every dashboard schedule entry. See
[deployments-and-eviction.md](../concepts/deployments-and-eviction.md).

## Processor

One per model key, owned by the dispatcher: an in-memory request queue, a pool of
**Replica** workers draining it, and an autoscaling loop
(`src/ndif/services/api/queue/processor.py` `Processor`). Provisions on first
request, re-provisions when a replica dies with work queued, never torn down. See
[queue-internals.md](../developing/queue-internals.md).

## Provider

NDIF's per-process wrapper around an external service (Redis, Ray, object store,
Postgres, Loki, InfluxDB), configured from a `CONFIG` dict of `NDIF_*` variables
(`src/ndif/common/providers/base.py` `Provider`). See
[providers.md](../developing/providers.md).

## Queue

The Redis list (`NDIF_QUEUE_KEY`, default `queue`) the API pushes pickled requests
onto and the dispatcher pops. Per-model queues exist only in the dispatcher's
memory, one per Processor. See
[queue-and-scheduling.md](../concepts/queue-and-scheduling.md).

## Replica

One serving instance of a model: on the Ray side a **Deployment** and its model
actor; on the API side a `Replica` (`src/ndif/services/api/queue/replica.py`
`Replica`) whose task dispatches to it. `DeploymentConfig.replicas` is additive.
See [queue-internals.md](../developing/queue-internals.md).

## Request

The unit of work, `BackendRequestModel` (`src/ndif/common/schema/request.py`):
nnsight's `RequestModel` plus a server-side `id`, identity, `trusted` / `priority`,
the `payload`, and status timing. See [schemas.md](schemas.md) and
[request-lifecycle.md](../concepts/request-lifecycle.md).

## Result delivery (inline or presigned)

A COMPLETED result small enough for the Redis channel
(`RedisProvider.max_publish_bytes`) rides on the response itself as a binary
websocket frame; a larger one, and every non-blocking one, is uploaded to the
object store and returned as a **presigned GET URL** signed for
`NDIF_OBJECT_STORE_PUBLIC_URL`. See
[status-and-results.md](../concepts/status-and-results.md).

## Runner

The OS process that executes an untrusted request's block,
`python -m ndif.services.ray.sandbox.runner`. One fresh runner per request,
stopped afterward; `Pool` (`src/ndif/services/ray/sandbox/host.py`) keeps
`NDIF_SANDBOX_POOL_SIZE` pre-warmed. It holds only a weightless meta model and
drives the host's real one over a Unix socket. See
[sandbox-execution.md](../concepts/sandbox-execution.md).

## Sandbox

The process split in `src/ndif/services/ray/sandbox/`: the block runs in a
**runner** while the model stays in the actor, interleaved over a socket. Process
separation, not a security boundary. See
[sandbox-execution.md](../concepts/sandbox-execution.md).

## Service

A long-running process NDIF starts: `redis`, `rustfs`, `ray`, `api` (the `all` set)
and the optional `dashboard`, each a name in `src/ndif/cli/service.py` plus a
start command. `NDIF_SERVICE` picks which and labels telemetry. See
[adding-a-service.md](../developing/adding-a-service.md).

## Trusted / untrusted

`BackendRequestModel.trusted`: trusted runs the block in the model actor process
and may deploy a model key asking for `trust_remote_code`; untrusted sends it to a
runner. `BackendRequestModel.trust_remote_code` is the deploy permission alone,
from the `trust_remote_code` tag (or `trusted`).
Stamped at ingress from the API key; with auth off, defaulted to trusted unless the
client sends `trusted: false`. See
[auth-and-limits.md](../concepts/auth-and-limits.md).

---

## nnsight terms

### Greenlet worker

A cooperative coroutine running one block of intervention code. nnsight interleaves
user code and the forward pass with greenlets, not threads. In the sandbox the
workers live in the runner.

### Host (sandbox sense)

The model actor process — weights and real hooks — as opposed to the **runner**
holding the user's code. Not a network host.

### Interleaver

nnsight's model-side driver: hooks every module and, as the forward reaches each
location, offers the value to the parked workers and takes back any edit. The
sandbox keeps it on the host.

### Mediator

The per-block half of the interleaver: one worker greenlet, occurrence counting,
the `tracer.iter` pin, and read/swap matching.

### MediatorProxy

NDIF-specific: the host-side half of a split mediator
(`src/ndif/services/ray/sandbox/driver.py` `MediatorProxy`). Subclasses nnsight's
`Mediator`, reuses its `handle`, and overrides only the boundary — `adopt` re-tags a
park from the runner, `switch` becomes a socket round-trip. One per worker. See
[sandbox-internals.md](../developing/sandbox-internals.md).

### save

`value.save()` / `nnsight.save(value)` marks a value to return. Server-side, saves
are collected, `torch.save`d with a CPU-relocating pickler, and sent back. The
method form comes from an optional C extension, so the image installs a compiler
(`gcc`, `libc6-dev` in `docker/Dockerfile`).

### Traced block

The body of `with model.trace(...):` — the user Python nnsight captures, ships, and
NDIF runs server-side.

### Tracer

The object `model.trace(...)` / `model.session(...)` yields; on the server, what is
deserialized from the payload and executed.

## Related

- [request-lifecycle.md](../concepts/request-lifecycle.md) — most of these terms in the order a request meets them.
- [deployments-and-eviction.md](../concepts/deployments-and-eviction.md) — deployment, level, replica, pinning, eviction in context.
- [sandbox-internals.md](../developing/sandbox-internals.md) — runner, host, park, MediatorProxy in detail.
