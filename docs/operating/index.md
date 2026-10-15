---
title: Operating NDIF
one_liner: Running an NDIF — from a first `docker run` or `just up` to a deployment that faces real users.
tags: [operating]
related: [docs/operating/quickstart.md, docs/operating/production.md, docs/runbooks/index.md, docs/reference/env-vars.md]
sources: []
---

# Operating NDIF

## What this covers

Running NDIF rather than changing it: bringing the stack up, configuring it,
deploying models, watching it, and hardening it before it faces users.
Step-by-step procedures live in [Runbooks](../runbooks/index.md).

## The shortest path

1. [Quickstart](quickstart.md) — the three routes (`docker run` from the published
   image, compose from the checkout, from source with the CLI), then a working
   `remote=True` trace.
2. [The Compose Stack](compose-stack.md) — what those ten containers are.
3. [Models and Deployment](models-and-deployment.md) — getting your models running.
4. [Going to Production](production.md) — everything that must change first.

## The pages

| Page | Read it when |
|---|---|
| [Quickstart](quickstart.md) | You want an NDIF of your own running, by any of the three routes. |
| [The Compose Stack](compose-stack.md) | You need to know what a container is for, or what breaks without it. |
| [Configuration](configuration.md) | You're wondering how a setting reaches a process. |
| [Models and Deployment](models-and-deployment.md) | Deploying, pinning, evicting, or sizing a model. |
| [The ndif CLI](cli.md) | You need the exact command and its flags. |
| [Admin Dashboard](dashboard.md) | Running the web UI, its auth, and its crons. |
| [Observability](observability.md) | Finding the logs, metrics, and dashboards. |
| [Going to Production](production.md) | Before anyone but you can reach it. |
| [Troubleshooting](troubleshooting.md) | Something is broken. The single entry point; it routes to the runbooks and error lookup. |

## Related

- [Runbooks](../runbooks/index.md) — step-by-step procedures.
- [Environment Variables](../reference/env-vars.md) — the exhaustive knob list.
- [Ports](../reference/ports.md) — what binds where, and what belongs public.
- [Services and Topology](../concepts/services-and-topology.md) — the mental model.
