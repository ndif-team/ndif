---
title: NDIF Concepts
one_liner: The mental models behind NDIF — read these before changing anything, or when a behavior seems inexplicable.
tags: [concepts]
related: [docs/developing/architecture-overview.md, docs/operating/quickstart.md, docs/reference/glossary.md]
sources: []
---

# NDIF Concepts

## What this covers

Seven pages of mental model. They explain *why* NDIF is shaped the way it is, so
that the reference and internals docs read as annotations rather than trivia. If
you are new to NDIF, read [Request Lifecycle](request-lifecycle.md) once, in
full — every other page in this folder is a zoom into one of its hops.

## The one-paragraph model

A researcher runs `with model.trace(prompt, remote=True):` against a model far too
large for their own hardware. nnsight builds that model on the meta device, so
**nothing about the model is in the request** — only a serialized *block* of the
user's intervention code and a `model_key` naming the model they want. NDIF's API
authenticates the request and pushes it onto a Redis list; a dispatcher process
routes it to a per-model queue, provisions a GPU replica if none is HOT, and hands
the block to the actor that holds the weights. The block and the model's forward
pass then run **interleaved** — the user's code parks whenever it reads or writes
an activation, the model runs until it reaches that point, the value crosses, and
the block resumes. Saved values are `torch.save`d to an object store and returned
as a presigned URL.

## Read in this order

1. **[Request Lifecycle](request-lifecycle.md)** — the keystone. One remote trace,
   every hop, plus a table of what goes wrong at each. Start here.
2. **[Services and Topology](services-and-topology.md)** — the three NDIF services
   and seven supporting containers; what talks to what, what holds state, what
   degrades when a piece is missing.
3. **[Queue and Scheduling](queue-and-scheduling.md)** — one Redis list feeding
   per-model in-memory queues in a single dispatcher process; when a second
   replica appears, and what "fair" does and doesn't mean here.
4. **[Deployments and Eviction](deployments-and-eviction.md)** — what "the model is
   deployed" actually means, the HOT/WARM/COLD levels, GPU accounting, pinning,
   and what the controller may throw away.
5. **[Sandbox Execution](sandbox-execution.md)** — why a user's block runs in a
   separate process from the model, and how the two stay in lockstep over a socket.
6. **[Status and Results](status-and-results.md)** — the status lifecycle as the
   client sees it, and why results come back as a blob behind a presigned URL.
7. **[Auth and Limits](auth-and-limits.md)** — API keys, what is and isn't
   enforced, and the one default that changes everything.

## The three facts that explain most surprises

**The model never moves; the code does.** The client ships source, not tensors.
This is why a missing import in the user's block fails at *deserialize* time on
the server, and why the server's nnsight version has to be compatible with the
client's.

**The API process cannot talk to Ray.** Only the dispatcher — a separate process
spawned by the gunicorn master — holds a Ray client. Every endpoint that appears
to know about the cluster is really reading a Redis-backed cache the dispatcher
refreshes. This explains the `/status` and `/env` cache handshakes, and why
`ray:connected` going stale makes the API confidently report health it cannot
actually verify.

**Where user code runs depends on one boolean.** `request.trusted` is stamped at
ingress from the API key. A trusted block runs *inside the model actor process*,
next to the weights; an untrusted one runs in a separate runner process driven
over a Unix socket. **With auth off — no `NDIF_POSTGRES_URL` — every request is
trusted**, so a default `just up` runs all user code in-process and loads models
with `trust_remote_code`. If you read one thing in this folder before exposing an
NDIF to anyone, make it [Auth and Limits](auth-and-limits.md).

## Related

- [Architecture Overview](../developing/architecture-overview.md) — the same system
  cut by process rather than followed by request.
- [Glossary](../reference/glossary.md) — every term used here, defined.
- [Quickstart](../operating/quickstart.md) — the shortest path to watching all of
  this happen on your own machine.
