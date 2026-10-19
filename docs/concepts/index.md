---
title: NDIF Concepts
one_liner: The mental models behind NDIF — read these before changing anything, or when a behavior seems inexplicable.
tags: [concepts]
related: [docs/developing/architecture-overview.md, docs/operating/quickstart.md, docs/reference/glossary.md]
sources: []
---

# NDIF Concepts

## What this covers

Seven pages of mental model: why NDIF is shaped the way it is. Read
[Request Lifecycle](request-lifecycle.md) first; every other page zooms into one
of its hops.

## Read in this order

1. **[Request Lifecycle](request-lifecycle.md)** — the keystone. One remote trace,
   every hop, plus a table of what goes wrong at each. Start here.
2. **[Services and Topology](services-and-topology.md)** — the three NDIF services
   and seven supporting containers; what talks to what, what holds state, what
   degrades when a piece is missing.
3. **[Queue and Scheduling](queue-and-scheduling.md)** — one Redis list feeding
   per-model in-memory priority queues in a single dispatcher process; when a
   second replica appears, and what "fair" does and doesn't mean here.
4. **[Deployments and Eviction](deployments-and-eviction.md)** — what "the model is
   deployed" actually means, the HOT/WARM/COLD levels, GPU accounting, pinning,
   and what the controller may throw away.
5. **[Sandbox Execution](sandbox-execution.md)** — why a user's block runs in a
   separate process from the model, and how the two stay in lockstep over a socket.
6. **[Status and Results](status-and-results.md)** — the status lifecycle as the
   client sees it, and how results come back: inline on the response, or behind a
   presigned URL.
7. **[Auth and Limits](auth-and-limits.md)** — API keys, the `trusted` flag end
   to end (its auth-off default, which actor classes honor it, what the untrusted
   path cannot do), the `trust_remote_code` tag and deploy gate, and what is and isn't enforced.

## Related

- [Architecture Overview](../developing/architecture-overview.md) — the same system
  cut by process rather than followed by request.
- [Glossary](../reference/glossary.md) — every term used here, defined.
- [Quickstart](../operating/quickstart.md) — the shortest path to watching all of
  this happen on your own machine.
