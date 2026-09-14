---
title: Reference
one_liner: Lookup tables — every endpoint, schema field, Redis key, environment variable, and port, sourced from the code.
tags: [reference]
related: [docs/reference/env-vars.md, docs/reference/http-api.md, docs/reference/schemas.md, docs/reference/glossary.md]
sources: []
---

# Reference

## What this covers

The lookup tree. These pages are exhaustive rather than narrative: each row is
derived from a line of code, with the file and line cited so you can verify it.
When a reference page and prose elsewhere disagree, the reference page is the one
built to be checked — but the **code** is always the authority.

## The pages

| Page | Answers |
|---|---|
| [HTTP API Reference](http-api.md) | What endpoints exist, who may call them, and what they return. |
| [Wire Schemas](schemas.md) | Every field of the request/response/controller models, and the Status enum. |
| [Redis Keys Reference](redis-keys.md) | Every key, channel and stream — type, writer, reader, TTL. |
| [Environment Variables](env-vars.md) | Every `NDIF_*` variable — default, reader, effect. |
| [Ports](ports.md) | What binds where, what compose publishes, what belongs public. |
| [Glossary](glossary.md) | What a term means, and which page covers it. |
| [External Resources](external-resources.md) | Where to go when the answer isn't in this repo. |

## Known discrepancies with the README

The reference pages were built from the code and then diffed against
`README.md`. Where they disagree, these pages document the code's actual behavior
and flag the difference. Two rows of the README's `NDIF_*` table still mislead:

- **`NDIF_API_KEY` is not read by the CLI.** `README.md:160` calls it "Client API
  key used by the `ndif` CLI"; nothing under `src/ndif/cli/` reads it. Its only
  reader is the dashboard's monitor cron, whose synthetic traces need a key to
  authenticate (`dashboard/jobs/monitor.py:404`, `dashboard/start.sh:65`).
- **`NDIF_CONTROLLER_SYNC_INTERVAL_S` does not reconcile deployments.**
  `README.md:200` calls it the "reconcile cadence for the deployment
  controller"; the loop it paces calls `Cluster.update_nodes()` and nothing else
  (`controller.py:161-165`), so it re-syncs the **node set**. Deployment changes
  are event-driven.

Two things the README gets right, listed because search results and older notes
still get them wrong: `NDIF_MODEL_CACHE_PERCENTAGE` scales **host RAM** and is
not a GPU knob (`README.md:202`), and `NDIF_RAY_METRICS_PORT` is Ray's
`--metrics-export-port` and has nothing to do with Ray Serve, which NDIF does not
use at all (`README.md:112, 188`).

## Related

- [Concepts](../concepts/index.md) — what these fields and endpoints are *for*.
- [Developing](../developing/index.md) — the subsystems that read them.
