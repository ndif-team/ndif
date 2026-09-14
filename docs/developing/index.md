---
title: Developing NDIF
one_liner: Internals reference for contributors and agents changing NDIF's code — one page per subsystem, plus the recipes for extending it.
tags: [internals, dev]
related: [docs/developing/architecture-overview.md, docs/developing/repo-layout.md, docs/concepts/index.md]
sources: []
---

# Developing NDIF

## What this covers

The internals tree. Pages here cite source `file:line`, describe data flow between
subsystems, and mark the extension points. It sits one level below
`docs/concepts/` (the mental models) and `docs/operating/` (running a cluster).

Audience: contributors, and agents whose user wants to change or debug the server.
If you want to *run* NDIF rather than modify it, start at
[Operating](../operating/index.md).

## The one-paragraph model

An API worker authenticates a request and `LPUSH`es it onto a Redis list — that is
the entire synchronous path. A **dispatcher**, a separate process spawned by the
gunicorn master and the only holder of a Ray client, pops it, routes it to a
per-model `Processor`, and provisions a replica if none is HOT. A **controller**
actor on the Ray head owns cluster state and decides placement and eviction.
Replicas are **detached Ray actors** — not Ray Serve deployments — each owning one
model's weights. The actor's `run()` is a template that races `execute()` against a
timeout; `execute()` either runs the user's block **in-process** (trusted) or ships
it to a **fresh runner subprocess** and drives the forward pass over a Unix socket
(untrusted). Results go to an object store; the client gets a presigned URL.

## Start here

- **[Architecture Overview](architecture-overview.md)** — the process map, the
  concurrency model, where state lives, and the boundaries not to cross.
- **[Repo Layout](repo-layout.md)** — directory by directory, and an
  "I want to change X, open Y" table.

## By subsystem

### Ingress and queueing
- [API Service](api-service.md) — the FastAPI app, gunicorn boot, the dependency
  chain, and how a request becomes a queue entry.
- [Queue Internals](queue-internals.md) — dispatcher, `Processor`, `Replica`,
  autoscaling, and the places the queue can wedge.

### The GPU side
- [Ray Service](ray-service.md) — head vs worker, every port, the custom resources
  a node advertises.
- [Controller Internals](controller-internals.md) — the Cluster/Node/Deployment/
  Evaluator model, sizing, placement, and eviction.
- [Model Actor](model-actor.md) — loading, the `run()` template, results, timeouts,
  metrics.
- [Sandbox Internals](sandbox-internals.md) — the untrusted path: the wire
  protocol, the split interleaver, and one proxy per worker. The densest page in
  the tree; the subsystem also has its own
  `src/ndif/services/ray/sandbox/ARCHITECTURE.md`.

### Shared foundations
- [Providers](providers.md) — the provider pattern and each backing service, with
  its fail-open behavior.
- [The Redis Layer](redis-layer.md) — the coalesced status/env caches, the event
  stream, response pub/sub.
- [Telemetry Internals](telemetry-internals.md) — logger tree, `event()`, every
  metric, and the fork-safe connect points.
- [nnsight Integration](nnsight-integration.md) — the client/server contract and
  which nnsight internals the server depends on.

### Operator surfaces
- [CLI Internals](cli-internals.md) — the click app and the `lib/` layer the
  dashboard also calls.
- [Dashboard Internals](dashboard-internals.md) — the FastAPI backend, stores, and
  cron jobs.
- [Dashboard Frontend](dashboard-frontend.md) — the Vue SPA and its build.

## Extending

- [Adding a Model Actor](adding-a-model-actor.md) — the five hooks that are the
  real extension points.
- [Adding a Provider](adding-a-provider.md) — the base-class contract and
  fail-open discipline.
- [Adding a Service](adding-a-service.md) — the `NDIF_SERVICE` + `start.sh` + CLI
  registry contract.

## Working on the code

- [Testing](testing.md) — the live-server suite, and how to force the untrusted
  path that local dev never exercises.
- [Contributing](contributing.md) — the conventions this codebase actually follows.

## Two things that catch everyone

**Local dev does not exercise the sandbox.** Two independent reasons: auth-off
stamps every request `trusted`, and the code-default actor class is the in-process
`ModelActor` (only compose sets `SandboxModelActor`). Both paths must produce
identical results — that invariant is why the sandbox is shaped as it is, and
[Testing](testing.md) shows how to actually check it.

**Telemetry providers connect at import and own threads.** They must be connected
*after* forking — `post_fork` for gunicorn workers, `spawn` for the dispatcher. A
new entry point that imports them too early gets neither console formatting nor
telemetry, silently.

## Proposals (not implemented)

- [Checkpoint Description](checkpoint-description-proposal.md) — the controller
  asks nnsight four separate questions before placing a model, fetches the same
  config twice doing it, and blocks its own event loop on un-timed-out Hub calls.
  The last of those is a live availability problem; the interface change is not.

## Related

- [Concepts](../concepts/index.md) — the mental models behind all of this.
- [Reference](../reference/index.md) — schemas, endpoints, Redis keys, env vars.
