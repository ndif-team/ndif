---
title: Developing NDIF
one_liner: Internals reference for contributors and agents changing NDIF's code — one page per subsystem, plus the recipes for extending it.
tags: [internals, dev]
related: [docs/developing/architecture-overview.md, docs/developing/repo-layout.md, docs/concepts/index.md]
sources: []
---

# Developing NDIF

## What this covers

The internals tree: one page per subsystem, citing source by path and symbol,
with the data flow between subsystems and the extension points. The mental
models are in [Concepts](../concepts/index.md).

Audience: contributors, and agents whose user wants to change or debug the server.
If you want to *run* NDIF rather than modify it, start at
[Operating](../operating/index.md).

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
- [Sandbox Internals](sandbox-internals.md) — the untrusted path, which aims to
  reproduce the trusted path's results: the wire protocol, the split interleaver,
  and one proxy per worker. The subsystem also has its own
  `src/ndif/services/ray/sandbox/ARCHITECTURE.md`.

### Shared foundations
- [Providers](providers.md) — the provider pattern and each backing service, with
  its fail-open behavior.
- [The Redis Layer](redis-layer.md) — the coalesced status/env caches, the event
  stream, response pub/sub.
- [Telemetry Internals](telemetry-internals.md) — logging, metrics, and when to
  connect the providers.
- [nnsight Integration](nnsight-integration.md) — the client/server contract and
  which nnsight internals the server depends on.

### Operator surfaces
- [CLI Internals](cli-internals.md) — the click app and the `lib/` layer the
  dashboard also calls.
- [Dashboard Internals](dashboard-internals.md) — the FastAPI backend, stores, and
  cron jobs.
- [Dashboard Frontend](dashboard-frontend.md) — the Vue SPA and its build.

## Extending

- [Adding a Model Actor](adding-a-model-actor.md) — the six hooks `run()` calls,
  plus the `commit()` seam.
- [Writing a new provider](providers.md#writing-a-new-provider) — the base-class contract and
  fail-open discipline.
- [Adding a Service](adding-a-service.md) — the `NDIF_SERVICE` + `start.sh` + CLI
  registry contract.

## Working on the code

- [Testing](testing.md) — the live-server suite, the files that run without a
  server, and how to force the untrusted path. No CI workflow runs the tests.
- [Contributing](contributing.md) — the conventions this codebase actually follows.

## Related

- [Concepts](../concepts/index.md) — the mental models behind all of this.
- [Reference](../reference/index.md) — schemas, endpoints, Redis keys, env vars.
