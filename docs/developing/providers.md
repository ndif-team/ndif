---
title: Providers
one_liner: common/providers/* — classmethod singletons over Redis, Ray, S3, Postgres, InfluxDB and Loki, configured purely from the environment, each with its own answer to "what if the service is down" — and how to add a new one.
tags: [internals, dev, redis, ray, telemetry, auth]
related: [docs/developing/redis-layer.md, docs/developing/telemetry-internals.md, docs/reference/env-vars.md, docs/reference/redis-keys.md, docs/concepts/services-and-topology.md, docs/concepts/auth-and-limits.md]
sources: [src/ndif/common/providers/base.py, src/ndif/common/providers/redis.py, src/ndif/common/providers/ray.py, src/ndif/common/providers/objectstore.py, src/ndif/common/providers/postgres.py, src/ndif/common/providers/influx.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/util.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/ray/queue/dispatcher.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py]
---

# Providers

## What this covers

Every external service NDIF talks to — Redis, Ray, an S3-compatible object store,
Postgres, InfluxDB, Loki — is reached through a *provider* in
`src/ndif/common/providers/`. This page covers the shared pattern, what each
provider wraps and how it fails, and how to write a new one. Every variable and
default is in [Environment variables](../reference/env-vars.md); this page does not
repeat them.

Two facts shape the layer. **There is no config file** — a provider declares the env
vars it reads, and that is the whole configuration mechanism. And **NDIF is a mesh of
separate processes** (gunicorn workers, a Ray controller actor, a dispatcher actor,
one actor per model replica), each of which establishes its own connections without
coordinating. Hence classmethod singletons that connect at import.

## The pattern

`src/ndif/common/providers/base.py` `Provider` is small. A subclass sets `CONFIG`,
a dict mapping an attribute name to `(ENV_VAR, typed default, caster)`:

```python
class RedisProvider(Provider):
    CONFIG = {"url": ("NDIF_REDIS_URL", "redis://localhost:6379", str)}
```

`Provider.from_env` walks that spec and sets class attributes (`RedisProvider.url`),
casting the raw env string or taking the typed default. `Provider.to_env` is the
inverse — `{ENV_VAR: str}` — and is how config crosses a process boundary.
Lifecycle is `connect` / `connected` / `reset` / `disconnect`, all classmethods;
nothing is instantiated. The base versions are permissive (no-ops, `connected()` is
`True`), so a provider overrides only what it needs. There is no base `ensure`;
`PostgresProvider.ensure` is its own, because its pool is built lazily inside an
event loop.

### Importing is connecting

Four modules end with `from_env(); connect()`, so **importing the module
establishes the connection in that process**: `redis.py` and `objectstore.py`
(cheap — no socket until the first command), `influx.py` and `loki.py` (each starts
a background shipper thread). `ray.py` and `postgres.py` only call `from_env()`:
`ray.init()` is expensive and only the CLI and the dashboard want it, and an asyncpg
pool needs a running event loop. So `providers/__init__.py` exports nothing —
importing the package must not connect you to services nobody asked for. Always
import the concrete module: `from ndif.common.providers.redis import RedisProvider`.

The two telemetry providers' threads make *where* a process imports them
load-bearing, because threads don't survive `fork()`. The rule and the gunicorn
wiring that follows it live in
[Telemetry internals](telemetry-internals.md#process-wiring).

### Config propagation

A Ray worker inherits only its node's ambient environment, so config is shipped
explicitly with `to_env()`. `Deployment.create` puts Redis, object-store, Loki and
Influx config into a model actor's `runtime_env`
(`src/ndif/services/ray/deployments/controller/cluster/deployment.py`
`_provider_runtime_env`) and overrides `NDIF_SERVICE` to `model`. The controller
actor is launched the same way with only `LokiProvider.to_env()`
(`src/ndif/services/ray/deployments/controller/controller.py` `app`), and the
dispatcher actor, which reads the head node's environment, with only
`NDIF_SERVICE=dispatcher` (`src/ndif/services/ray/queue/dispatcher.py` `app`).

## What runs with no configuration

NDIF runs end to end with **no** provider configuration: Redis, the object store and
Ray have localhost defaults, and the three optional providers are off.

| Provider | Turned on by | On | Off |
|---|---|---|---|
| Loki | `NDIF_LOKI_URL` | the `ndif` logger also ships JSON lines to Loki | console logging only |
| InfluxDB | `NDIF_INFLUX_URL` (+ token; `NDIF_INFLUX_ENABLED` not false) | metrics are batched to InfluxDB | every `Metric.update(...)` is a no-op |
| Postgres | `NDIF_POSTGRES_URL` | API-key auth; the request's `tags` are the key's user_tags | **no auth**, and requests are trusted unless the client sends `tags: []` |

Loki and InfluxDB are best-effort telemetry and fail open — their absence costs
observability and nothing else; the exact fallbacks are in
[Telemetry internals](telemetry-internals.md#fail-open-behavior). The Postgres row
is the high-consequence one: a trusted request runs the caller's Python in the model
actor process, and may deploy a model whose key carries `trust_remote_code`.
[Auth and limits](../concepts/auth-and-limits.md) is the home for those
consequences.

The three *required* providers have no fallback: Redis raises at the call site, Ray
raises `ConnectionError` to the CLI or dashboard call that connects, and the object
store raises on `put`/`presigned_get`. Postgres **fails closed** — a query failure is a
503, never an allowed request.

## RedisProvider

`src/ndif/common/providers/redis.py`. Three client singletons off one URL, built in
`RedisProvider.connect`:

- `sync_client` — sync, `decode_responses=True`: response pub/sub from sync code
  (the model actor).
- `async_client` — async, `decode_responses=True`: the status/env caches, the
  events stream, the dispatcher's `ray:connected` heartbeat, response pub/sub from
  async workers.
- `async_bytes_client` — async, raw bytes: the pickled request queue (decoding
  would corrupt the pickle).

All three pass `socket_timeout` explicitly: `None` on `sync_client`, and
`RedisProvider.async_socket_timeout`, `None` by default, on the async two. That is
load-bearing: redis-py 8+ negotiates "maintenance notifications" with Redis 8 and
silently sets a 5 s socket timeout, shorter than the dispatcher's
`brpop(..., timeout=10)`, so every idle dispatch iteration would raise a spurious
`TimeoutError`.

Only the dispatcher sets `async_socket_timeout`: `DispatcherActor.__init__`
(`src/ndif/services/ray/queue/dispatcher.py`) makes it the fetch timeout plus 10 s
and calls `connect()` again, so a read on a connection that died without closing
raises `redis.exceptions.TimeoutError` and the loop retries. Every other process
keeps `None`: a pubsub subscriber waits on a quiet channel for as long as it
stays quiet.

Only `sync_client` retries a dead connection, three times with backoff, because a model
actor writes its result through it. The async clients stay without retries: a pubsub
subscriber that reconnected silently would miss whatever was published meanwhile and wait
forever, rather than fail.

`RedisProvider.connected` is a `ping()` with exceptions swallowed.

**Used by** the API app, the dispatcher, and `BackendRequestModel.respond` /
`arespond` — every process that publishes a status update, model actors included.
See [the Redis layer](redis-layer.md).

## RayProvider

`src/ndif/common/providers/ray.py`. The Ray Client connection plus named-actor
lookups. Only the CLI and the dashboard connect over Ray Client (`ray://`). The
dispatcher is an actor inside the cluster, with no connection to manage, and API
workers never touch Ray, which is what produces the whole Redis cache layer. A
`NDIF_RAY_ADDRESS` with no port falls back to 6379 with a warning
(`RayProvider.get_host_port`).

`RayProvider.connected` is stricter than `ray.is_initialized()`: it also requires a
TCP connect to the address (`src/ndif/common/providers/util.py`
`verify_connection`, 2 s) **and** that the `Controller` actor exists in the `NDIF`
namespace. It does not call the Controller, which may be busy: the lookup goes to
the head over the channel every call uses, and is answered without it.

The module also holds `CachedActorError`, raised when a dispatch lands on an actor
moved to CPU (WARM), and two kinds of handle to the actors `"Controller"` and
`"{replica_id}:ModelActor:{model_key}"` in namespace `NDIF`:

- `get_named_actor`, `get_controller_actor_handle` and `get_model_actor_handle`
  return an `NDIFActorHandle`, a `ClientActorHandle` that skips stock Ray's
  first-access signature RPC — that RPC unpickles annotations client-side and drags in
  `BackendRequestModel`, which breaks a slim `--no-deps` install. These block their
  caller, and are what the CLI uses.
- `controller_handle` and `model_handle` return Ray's own handle, from
  `ray.get_actor`, for the queue, which is inside the cluster.
  `await handle.method.remote(...)` yields to the event loop; a missing actor raises
  `ValueError` from the lookup, and a dead one `ActorDiedError` from the call.

Every lookup and call through the client handles is bounded at
`RAY_CLIENT_TIMEOUT_S` (60 s) and raises `TimeoutError` past it. Over Ray Client an
actor lookup and the id of a call wait with no timeout when the head dies, so each
runs on a thread the caller gives up on at the limit (`bounded`): the lookup and
each `.remote()` have the limit each. The result of a call has no limit. The
queue's handles are not over Ray Client and have no limit of their own.

`ray.init` has no limit: a head that accepts the connection and then stalls holds
the caller there until that head goes. `reset()` closes the gRPC channel before
`ray.shutdown()`, which would otherwise wait on a dead head; the head is then not
told to drop the session, and keeps its per-client server for Ray's 30 s grace
period.

**Used by** the CLI's `ensure_ray_connected` (`src/ndif/cli/lib/_common.py`), which
overwrites `RayProvider.ray_url` from `--ray-address` first, and through the CLI's
`lib/` by the dashboard.

## ObjectStoreProvider

`src/ndif/common/providers/objectstore.py`. boto3 over S3, so the same code serves
RustFS and AWS S3. Results over the inline cap go here and the client gets a presigned
GET URL.

Two clients, because upload and download happen from different networks
(`ObjectStoreProvider.connect`). A presigned URL is an HMAC over the request
*including the host*, so it must be signed with the host the downloader will hit —
`rustfs:9000` inside compose, `localhost:9000` or a public name outside. That is
`NDIF_OBJECT_STORE_PUBLIC_URL`; unset, it falls back to the server-side URL. A custom
endpoint also forces path-style addressing (`_make_client`). Credentials are passed
only when both are set; both empty uses boto3's own chain (an IAM role on AWS).
Methods: `put(key, data, content_type)` creates the bucket on first use and uploads
through `upload_fileobj` with `_TRANSFER` (boto3's defaults, 8 parts in flight):
one `put_object` up to 8 MiB, a parallel multipart upload above; `get(key)`
returns `bytes | None` and deliberately doesn't create it (missing means "nothing
yet"); `presigned_get(key, expires=1h)`. `max_socket_result_bytes`
(`NDIF_MAX_SOCKET_RESULT_BYTES`, 8 MiB) is the inline cap: the result size above
which `BaseModelDeployment.run` uploads here; what the cap means for a user is in
[Status and results](../concepts/status-and-results.md).

**Used by** `BaseModelDeployment.upload_bytes` (key `{request.id}.pt`),
`BackendRequestModel.respond` for non-blocking requests (key
`responses/{request_id}.json`), and `GET /response/{id}`.

## PostgresProvider

`src/ndif/common/providers/postgres.py`. An async `asyncpg` pool over the user/keys
database (schema in `docker/postgres/init.sql`). A generic sink — the auth *policy*
is `src/ndif/services/api/auth.py`, its only user.

`PostgresProvider.enabled` is `bool(cls.url)` with no I/O; `verify_api_key`
short-circuits on it. `connect()` is async and idempotent and builds the pool under
an `asyncio.Lock` with a double-check, so concurrent first requests create one pool;
`fetch` / `fetchrow` / `fetchval` each `await ensure()` first.

Optional-dependency handling is the **opposite** of the telemetry providers.
`asyncpg` is imported in a `try` so the module always imports, but with a URL set and
the package missing, `connect()` raises `RuntimeError` naming the `postgres` extra —
silently disabling auth would be a security hole. Likewise a query failure in auth
becomes a 503, never an unverified request.

## InfluxProvider and LokiProvider

`src/ndif/common/providers/influx.py` and `src/ndif/common/providers/loki.py`. The
telemetry sinks: numeric time series for Grafana, and the `ndif` logger shipped as
JSON lines via `python-logging-loki`'s `LokiQueueHandler`. `NDIF_SERVICE` and
`NDIF_ENVIRONMENT` become base tags/labels on everything. `InfluxProvider.write` is
the only Influx method call sites use (through `Metric._emit`); `LokiProvider.connect`
always configures the console first, then attaches Loki only when a URL is set.
What they emit, how they fail open, and where each process connects them is
[Telemetry internals](telemetry-internals.md).

## Writing a new provider

A new backing service — another object store, a second metrics sink, a message
bus — follows the pattern above. The part to get right is failure.

**`CONFIG` entries.** The key is the attribute name; the variable is `NDIF_`-prefixed
and grouped by subsystem (`NDIF_MYTHING_*`); the default is already typed (`10.0`,
not `"10.0"`), since the caster applies only when the variable is set; booleans go
through a `_boolish` caster, because `bool("false")` is `True`. Comment each entry
with what it does and what empty means — the object store's `CONFIG` is the model.
Empty means off for an optional subsystem; add an `_ENABLED` switch only to disable
something that *is* configured, as `NDIF_INFLUX_ENABLED` does.

**`connect()` cheap and idempotent; `connected()` a real probe.** Every entry point
calls `connect()` without coordinating, so a second call must be a no-op, and it
should open no socket until first use. `connected()` should reach the service, not
report that an object exists. `to_env()` must round-trip everything a model actor
needs. A provider that owns a thread says so in its `connect` docstring and follows
the fork rule in [Telemetry internals](telemetry-internals.md#process-wiring).

**Fail open — or say why not.** The test: *if this call silently did nothing, would
the system be degraded-but-correct, or quietly wrong?* Degraded → fail open, as the
telemetry sinks do: guard the import (the module must still import —
`ndif.common.metrics` imports the Influx provider in every service), wrap every call
path, log at **debug** so a flapping server doesn't write a line per flush, and bound
every queue and retry. Quietly wrong → raise, as Postgres does; Redis and the object
store are the request path and don't fail open at all.

A minimal fail-open sink, StatsD over UDP:

```python
try:
    import statsd
    _HAS_STATSD = True
except Exception:
    statsd, _HAS_STATSD = None, False

class StatsdProvider(Provider):
    CONFIG = {
        # Empty (the default) disables StatsD entirely.
        "url": ("NDIF_STATSD_URL", "", str),
        "service": ("NDIF_SERVICE", "unknown", str),
    }
    client = None

    @classmethod
    def connect(cls) -> None:
        if not cls.url or not _HAS_STATSD or cls.client is not None:
            return
        try:
            parsed = urlparse(cls.url)
            cls.client = statsd.StatsClient(parsed.hostname, parsed.port or 8125,
                                            prefix=f"ndif.{cls.service}")
        except Exception:
            logger.debug("StatsD telemetry unavailable", exc_info=True)

    @classmethod
    def incr(cls, name: str, count: int = 1) -> None:
        if cls.client is None:
            return
        try:
            cls.client.incr(name, count)
        except Exception:
            logger.debug("StatsD emit failed", exc_info=True)

StatsdProvider.from_env()
StatsdProvider.connect()
```

Checklist:

- [ ] `src/ndif/common/providers/<name>.py` with a module docstring that states the
  design constraint (opt-in? fail-open? fork-sensitive?) before the mechanism.
- [ ] `CONFIG` as above; `connect` cheap and idempotent; `connected` a real probe.
- [ ] Optional package guarded or lazily imported; failures at debug; queues and
  retries bounded — or a written reason to raise.
- [ ] `from_env(); connect()` at the bottom of the module; nothing added to
  `providers/__init__.py`.
- [ ] An extra and its image wiring — see
  [Adding a service](adding-a-service.md#dependencies-extras-and-the-image).
- [ ] Each new variable in [Environment variables](../reference/env-vars.md).

## Gotchas

- **Defaults are localhost.** Correct for `ndif start` on one host, wrong inside
  compose, where each container needs `NDIF_REDIS_URL=redis://redis:6379`,
  `NDIF_OBJECT_STORE_URL=http://rustfs:9000` and so on (set per service in
  `docker/docker-compose.yml`). A container that forgets them reaches for itself.
- **The object store's two URLs.** With `NDIF_OBJECT_STORE_PUBLIC_URL` unset,
  presigned URLs carry the server-side host and a client outside that network can't
  resolve them. Symptom: jobs COMPLETE but the download fails.

## Related

[The Redis layer](redis-layer.md) for what the Redis clients carry, and
[Redis keys](../reference/redis-keys.md) for every key, channel and stream.
[Auth and limits](../concepts/auth-and-limits.md) for how the Postgres provider
becomes API-key auth.
