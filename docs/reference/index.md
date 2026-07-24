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
and flag the difference. The ones most likely to mislead:

- **`NDIF_MODEL_CACHE_PERCENTAGE` scales host RAM**, not GPU memory — it is the
  WARM cache budget. The README calls it GPU memory.
- **`NDIF_RAY_METRICS_PORT` is not a Ray Serve port.** It is Ray's
  `--metrics-export-port`, the Prometheus scrape target. NDIF does not use Ray
  Serve at all; model deployments are detached Ray actors.
- **`NDIF_API_KEY` is not read by the CLI**, despite the README saying so; only
  the dashboard's monitor cron uses it.
- **`NDIF_CONTROLLER_SYNC_INTERVAL_S` does not reconcile deployments** — it
  re-syncs the node set. Deployment changes are event-driven.
- **`NDIF_RAY_HEAD_PORT`** defaults to `6385` both via the CLI and via
  `start.sh`'s own fallback, deliberately offset from Redis's 6379.

## Related

- [Concepts](../concepts/index.md) — what these fields and endpoints are *for*.
- [Developing](../developing/index.md) — the subsystems that read them.
