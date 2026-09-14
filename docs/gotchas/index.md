---
title: Gotchas
one_liner: The traps — things that are true, non-obvious, and will cost you an afternoon.
tags: [gotchas]
related: [docs/gotchas/networking-and-compose.md, docs/gotchas/gpu-and-memory.md, docs/gotchas/client-server-versions.md, docs/errors/index.md]
sources: []
---

# Gotchas

## What this covers

Three pages of traps, grouped by where they bite. Unlike [Errors](../errors/index.md),
most of these don't produce a clean error message — they produce confusing
behavior, or silent success that turns out to be wrong.

- **[Networking and Compose](networking-and-compose.md)** — service names vs
  localhost, the Redis/Ray GCS port collision, presigned URLs signed for the wrong
  host, health endpoints that lie.
- **[GPU and Memory](gpu-and-memory.md)** — `shm_size`, the two separate memory
  budgets, how a model is sized before placement, and eviction rules that block a
  deploy you expected to succeed.
- **[Client/Server Versions](client-server-versions.md)** — the nnsight coupling:
  version gating, the shared `Status` enum, source-not-bytecode serialization, and
  client-side packages that must match the server's model.

## The five that catch people most often

**Auth off means trusted by default.** No `NDIF_POSTGRES_URL` ⇒ an unspecified
`trusted` defaults to `True` ⇒ user code runs in the model actor process and models
load with `trust_remote_code`. It's the default, and it's silent — but a
client-supplied `trusted` is honored, so `trusted: false` opts out.

**Local dev doesn't exercise the sandbox by default.** Two independent reasons —
auth-off defaults to trusted, and the code-default actor class is the in-process
one. Code that works on your laptop may be taking a different path in production.
You can still force the runner path in dev by sending `trusted: false` — no
Postgres needed.

**`NDIF_MODEL_CACHE_PERCENTAGE` is host RAM, not GPU.** It sizes the WARM cache.
Turning it down to free GPU memory does nothing.

**Inside the `ray` container, `localhost:6379` is Ray's own GCS, not Redis.** This
is why the compose file sets `NDIF_REDIS_URL` explicitly on that service, and it's
a classic wrong turn while debugging.

**The dashboard SPA is committed.** `frontend/dist/` is checked in, so a clean
clone + `just up` serves the UI with no host-side build. You only need to rebuild
the SPA if you change the frontend.

## Related

- [Errors](../errors/index.md) — when the trap does produce a message.
- [Troubleshooting](../operating/troubleshooting.md) — symptom-first triage.
- [Concepts](../concepts/index.md) — the models that make these predictable.
