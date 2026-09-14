---
title: Operating NDIF
one_liner: Running an NDIF — from a first `docker run` or `just up` to a deployment that faces real users.
tags: [operating]
related: [docs/operating/quickstart.md, docs/operating/production.md, docs/runbooks/index.md, docs/reference/env-vars.md]
sources: []
---

# Operating NDIF

## What this covers

Everything about running NDIF rather than changing it: bringing the stack up,
configuring it, deploying models, watching it, and hardening it before it faces
users. Procedures that are long enough to need their own page live in
[Runbooks](../runbooks/index.md); this tree is the reference behind them.

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
| [Troubleshooting](troubleshooting.md) | Something is broken and you want triage, not theory. |

## Four things to know before you start

**It runs with zero configuration — and that default is not safe on a network.**
Every optional backing service is off until you set its URL, and the stack works
end to end without any of them. But an NDIF with no `NDIF_POSTGRES_URL` is
unauthenticated, which stamps **every unspecified request `trusted`**:
user-submitted Python runs in-process next to the model weights, and models load
with `trust_remote_code`. That is the intended default for one you run for
yourself, and unacceptable on a shared machine. See
[Auth and Limits](../concepts/auth-and-limits.md) and
[Enable API-Key Auth](../runbooks/enable-auth.md).

**Almost nothing persists.** Only the dashboard's state is a named volume. Result
blobs, metrics, logs, Postgres data, and downloaded model weights all vanish on
`just down`.

**The dashboard ships its UI.** `src/ndif/services/dashboard/frontend/dist/` is
committed — `.gitignore` re-includes it explicitly past the blanket `dist/` rule —
and it is packaged into the wheel (`pyproject.toml:147-152`), so a clean clone or
a plain `pip install` serves the SPA with no host-side npm build. Rebuild it only
when you change the frontend. See [Admin Dashboard](dashboard.md).

**Two GPU-adjacent numbers are easy to confuse.** GPU memory funds the resident
(HOT) model; `NDIF_MODEL_CACHE_PERCENTAGE` scales **host RAM** for the WARM cache.
Reaching for the latter during a GPU OOM is the wrong lever — see
[GPU and Memory](../gotchas/gpu-and-memory.md).

## Related

- [Runbooks](../runbooks/index.md) — step-by-step procedures.
- [Environment Variables](../reference/env-vars.md) — the exhaustive knob list.
- [Ports](../reference/ports.md) — what binds where, and what belongs public.
- [Services and Topology](../concepts/services-and-topology.md) — the mental model.
