---
title: NDIF Documentation
one_liner: Start here — find the page for your task, or the section for your question.
tags: [index]
related: [docs/concepts/index.md, docs/operating/index.md, docs/runbooks/index.md, docs/reference/index.md, docs/developing/index.md]
sources: []
---

# NDIF Documentation

NDIF is the server behind [nnsight](https://nnsight.net)'s `remote=True`. These
pages cover running it, operating it, and changing its code.

## By task

| You want to | Start at | Then |
|---|---|---|
| Run your own NDIF | [Quickstart](operating/quickstart.md) | [Compose stack](operating/compose-stack.md), [Configuration](operating/configuration.md), [Kubernetes](operating/kubernetes.md) |
| Deploy, pin, evict or size a model | [Models and deployment](operating/models-and-deployment.md) | [Deploy and pin](runbooks/deploy-and-pin-a-model.md), [Model OOM on deploy](runbooks/model-oom-on-deploy.md) |
| Fix something that broke | [Troubleshooting](operating/troubleshooting.md) | it routes to the runbooks and [Error messages](reference/error-messages.md) |
| Secure it, or expose it to other people | [Going to production](operating/production.md) | [Enable auth](runbooks/enable-auth.md), [Auth and limits](concepts/auth-and-limits.md) |
| Observe it — logs, metrics, the admin UI | [Observability](operating/observability.md) | [Dashboard](operating/dashboard.md), [Telemetry internals](developing/telemetry-internals.md) |
| Grow the cluster | [Add a GPU node](runbooks/add-a-gpu-node.md) | [Ray service](developing/ray-service.md) |
| Understand how a request runs | [Request lifecycle](concepts/request-lifecycle.md) | [Sandbox execution](concepts/sandbox-execution.md) |
| Change or extend the code | [Developing](developing/index.md) | [Architecture overview](developing/architecture-overview.md), [Repo layout](developing/repo-layout.md), [CONTRIBUTING.md](../CONTRIBUTING.md) |
| Answer a client-side question (API keys, `.save()`, sessions) | [nnsight.net](https://nnsight.net) | [nnsight integration](developing/nnsight-integration.md) for the client/server contract |

## Sections

- [Concepts](concepts/index.md) — the mental models: lifecycle, topology, queue, deployments, sandbox, status, auth.
- [Operating](operating/index.md) — running NDIF: setup, configuration, models, CLI, dashboard, observability, production.
- [Runbooks](runbooks/index.md) — one procedure per page, with commands and checks.
- [Reference](reference/index.md) — lookup tables: endpoints, schemas, Redis keys, env vars, ports, error messages.
- [Developing](developing/index.md) — internals, one page per subsystem, and how to extend each.
