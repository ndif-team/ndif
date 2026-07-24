---
title: Providers
one_liner: common/providers/* — classmethod singletons over Redis, Ray, S3, Postgres, InfluxDB and Loki, configured purely from the environment, each with its own answer to "what if the service is down".
tags: [internals, dev, redis, ray, telemetry, auth]
related: [docs/developing/redis-layer.md, docs/developing/adding-a-provider.md, docs/developing/telemetry-internals.md, docs/reference/env-vars.md, docs/reference/redis-keys.md, docs/concepts/services-and-topology.md, docs/concepts/auth-and-limits.md]
sources: [src/ndif/common/providers/base.py, src/ndif/common/providers/redis.py, src/ndif/common/providers/ray.py, src/ndif/common/providers/objectstore.py, src/ndif/common/providers/postgres.py, src/ndif/common/providers/influx.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/util.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py]
---

# Providers

## What this covers

Every external service NDIF talks to — Redis, Ray, an S3-compatible object store,
Postgres, InfluxDB, Loki — is reached through a *provider* in
`src/ndif/common/providers/`. This page covers the shared pattern, then each
provider: what it wraps, its env vars, its public surface, what happens when the
service is missing or down, and who uses it.

Two facts shape the layer. **There is no config file** — a provider declares the
env vars it reads and that is the entire configuration mechanism. And **NDIF is a
mesh of separate processes** (gunicorn workers, a spawned dispatcher, a Ray
controller actor, one actor per model replica), each of which must establish its
own connections without coordinating. Hence classmethod singletons that connect at
import.

## The pattern

`Provider` (`src/ndif/common/providers/base.py:26`) is small. A subclass sets
`CONFIG`, a dict mapping an attribute name to `(ENV_VAR, typed default, caster)`:

```python
class RedisProvider(Provider):
    CONFIG = {"url": ("NDIF_REDIS_URL", "redis://localhost:6379", str)}
```

`from_env()` (`base.py:39`) walks that spec and sets class attributes —
`RedisProvider.url` — casting the raw env string or taking the typed default.
`to_env()` (`base.py:46`) is the inverse, rendering the current values back into an
`{ENV_VAR: str}` dict; that is how config crosses a process boundary.

Lifecycle: `connect` / `connected` / `reset` / `disconnect`. All classmethods;
nothing is instantiated — call `RedisProvider.sync_client` or
`ObjectStoreProvider.put(...)` directly. Base implementations are permissive
(`connect`/`disconnect`/`reset` no-op, `connected()` returns `True`), so a provider
overrides only what it needs. The base `ensure` has no in-tree callers today —
`PostgresProvider` defines its own async version.

### Importing is connecting

Four modules run `from_env(); connect()` at the bottom of the file, so **importing
the module establishes the connection in that process**: `redis.py:72` and
`objectstore.py:171` (cheap — the clients open no socket until the first command),
`influx.py:224` and `loki.py:231` (both start a background shipper thread). Two
only load config: `ray.py:130` (`ray.init()` is expensive, and only the dispatcher
and CLI want it) and `postgres.py:158` (an asyncpg pool needs a running event loop,
which doesn't exist at import). Hence `providers/__init__.py` exports only the base
class — importing the package must not connect you to services nobody asked for.
Always import the concrete module:
`from ndif.common.providers.redis import RedisProvider`.

> **Gotcha:** Loki and InfluxDB each own a background thread, and threads do not
> survive `fork()` — importing them *before* forking hands every child a dead
> shipper. The gunicorn config imports neither at module level: `post_fork`
> (`gunicorn_conf.py:35`) imports them inside each worker, and the dispatcher is
> `spawn`ed, not forked, via a lazily-importing target (`gunicorn_conf.py:46`).

### Config propagation

A Ray worker inherits only its node's ambient environment, so config must be shipped
explicitly — that's what `to_env()` is for. `Deployment.create` exports Redis,
object-store, Loki and Influx config into a model actor's `runtime_env`
(`_provider_runtime_env`, `.../cluster/deployment.py:37`), then overrides
`NDIF_SERVICE` to `"model"` so the actor's telemetry attributes to the model, not
the controller (`deployment.py:187`). The controller actor is launched the same way
with just `LokiProvider.to_env()` (`controller.py:578`).

## Zero configuration, and what each URL turns on

NDIF runs end to end with **no** provider configuration at all: Redis, the object
store and Ray have working localhost defaults, and the three optional providers are
off. Each optional URL turns exactly one thing on.

| Provider | Set this to enable | With it set | Without it |
|---|---|---|---|
| Loki | `NDIF_LOKI_URL` | The `ndif` logger also ships structured JSON to Loki | Console logging only. Nothing else changes; the package need not be installed |
| InfluxDB | `NDIF_INFLUX_URL` + `NDIF_INFLUX_TOKEN` (and `NDIF_INFLUX_ENABLED` left at `true`) | Metrics are batched to InfluxDB for Grafana | Every `Metric.update(...)` is a silent no-op |
| Postgres | `NDIF_POSTGRES_URL` | API-key auth is enforced; `trusted` and `priority` come from the key's user_tags | **No auth, and every request is marked trusted** — see below |

The Loki and InfluxDB rows are inconsequential: best-effort telemetry, whose
absence costs observability and nothing else. The Postgres row is not.

### The Postgres default has two other consequences

With `NDIF_POSTGRES_URL` unset, `validate_request` defaults
`request.trusted` to `True` on an incoming request that doesn't set it — a
client-supplied `trusted: false` is honored (`src/ndif/services/api/auth.py:184`).
`trusted` is not only an auth outcome — it is read in two other places:

- `SandboxModelDeployment.execute` (`src/ndif/services/ray/sandbox/model.py:242`)
  returns `super().execute(request)` for a trusted request — the caller's traced
  Python runs **in-process inside the model actor, next to the model weights**,
  instead of in a separate runner subprocess.
- `Cluster.deploy` passes it as `trust_remote_code=config.trusted` when sizing and
  loading a model (`.../controller/cluster/cluster.py:169`), so models load with
  Hugging Face `trust_remote_code` enabled.

So "no Postgres ⇒ unauthenticated" is really "⇒ unauthenticated **and** every
caller's arbitrary Python runs unsandboxed **and** models load with
`trust_remote_code`". Sandboxing here is process-based (a separate runner process),
and is still in progress. If you are self-hosting an NDIF that anyone but you can
reach, configure Postgres.

The three *required* providers have no fallback: Redis raises at the call site, Ray
raises `ConnectionError` and the dispatcher retries every second, and the object
store raises on `put`/`presigned_get`. Postgres **fails closed** (a query failure
is a 503, never an allowed request); InfluxDB and Loki **fail open**.

## RedisProvider

`src/ndif/common/providers/redis.py`. Wraps `redis-py` with **three** client
singletons off one URL (`NDIF_REDIS_URL`, default `redis://localhost:6379`),
constructed in `connect()` (`redis.py:44`):

- `sync_client` — sync, `decode_responses=True`. Response pub/sub from sync code
  (the model actor); the dispatcher's `ray:connected` flag.
- `async_client` — async, `decode_responses=True`. The status/env caches, the events
  stream, response pub/sub from async workers.
- `async_bytes_client` — async, raw bytes. The pickled request queue; decoding
  would corrupt the pickle.

`RedisProvider.connected` (`redis.py:55`) is a `ping()` with exceptions swallowed.
`reset()` closes only the sync client; the async ones are replaced on the next
`connect()`, since closing them needs an event loop `reset()` may not be in. All
three pass `socket_timeout=None` explicitly, and that is load-bearing: redis-py
8.0+ negotiates a "maintenance notifications" feature with Redis 8 and silently
sets `socket_timeout=5`, shorter than the dispatcher's `brpop(queue, timeout=10)` —
without it, every idle dispatch iteration raises a spurious `TimeoutError`
(`redis.py:32`).

**Used by:** the API app (`app.py:114`, `:180`, `:221`, `:385`), the dispatcher, and
`BackendRequestModel.respond` / `arespond` (`common/schema/request.py:149`, `:173`)
— every process that publishes a status update, model actors included. See
[the Redis layer](redis-layer.md).

## RayProvider

`src/ndif/common/providers/ray.py`. The Ray control plane plus named-actor lookups.
One var: `NDIF_RAY_ADDRESS`, default `ray://localhost:10001`. Only the dispatcher
and the CLI connect to Ray — API workers never do, and that constraint is what
produces the whole Redis cache layer. (A URL with no port falls back to **6379**,
Redis's port, with a warning: `RayProvider.get_host_port`, `ray.py:51`.)

`RayProvider.connected` (`ray.py:90`) is stricter than `ray.is_initialized()`: it
requires initialization, a TCP connect to the parsed host/port (`verify_connection`,
`providers/util.py:4`, 2s timeout), **and** that the `Controller` actor exists in
the `NDIF` namespace — so the dispatcher's connect loop won't proceed until the
control plane is serving. `RayProvider.is_connection_error` (`ray.py:120`)
string-matches an exception against `CONNECTION_ERROR_PATTERNS` (`ray.py:108`); a
match triggers purge-and-reconnect (`Dispatcher.handle_errors`, `dispatcher.py:181`).

The module also holds the actor lookups `get_named_actor` /
`get_controller_actor_handle` / `get_model_actor_handle` (`ray.py:199`, `:212`,
`:217`), which name actors `"Controller"` and
`"{replica_id}:ModelActor:{model_key}"` in namespace `NDIF`; `CachedActorError`
(`ray.py:25`), raised when a dispatch lands on an actor moved to CPU cache (WARM);
and `NDIFActorHandle` (`ray.py:181`), a lean `ClientActorHandle` that skips stock
Ray's first-access RPC for method signatures — that RPC unpickles annotations
client-side, dragging in `BackendRequestModel` and its deps, which breaks on a slim
`--no-deps` install. `handle.method.remote(...)` is unchanged.

**Used by:** `Dispatcher.connect` (`dispatcher.py:97`) and `ensure_ray_connected`
(`cli/lib/_common.py:23`), which overwrites `RayProvider.ray_url` from
`--ray-address` before connecting.

## ObjectStoreProvider

`src/ndif/common/providers/objectstore.py`. boto3 over S3, so the same code serves
MinIO in dev and AWS S3 in prod. Result blobs are far too large for the Redis
response channel, so the model actor uploads them here and returns a presigned GET
URL on the COMPLETED response.

| Env var | Default | What it does |
|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | Server-side endpoint (upload, bucket ops). Empty → boto3 derives the real AWS endpoint from `region` |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | `""` | Client-facing endpoint used *only* for presigning. Empty → falls back to `url` |
| `NDIF_OBJECT_STORE_ACCESS_KEY` / `_SECRET_KEY` | `minioadmin` / `minioadmin` | Credentials for both clients |
| `NDIF_OBJECT_STORE_BUCKET` | `ndif-results` | Bucket for result blobs and non-blocking responses |
| `NDIF_OBJECT_STORE_REGION` / `_VERIFY` | `us-east-1` / `true` | Region is explicit so presigning never round-trips to discover it; set verify false for self-signed MinIO over https |

Two clients, because upload and download happen from different networks
(`ObjectStoreProvider.connect`, `objectstore.py:92`). A presigned URL is a local
HMAC over the request *including the host*, so it must be signed with the host the
downloader will hit — `minio:9000` from the compose network, `localhost:9000` from
the user's machine. A custom endpoint also forces path-style addressing, since
`bucket.minio:9000` wouldn't resolve (`_make_client`, `objectstore.py:67`). Methods:
`put(key, data, content_type)` (creates the bucket on first use,
`objectstore.py:97`), `get(key)` → `bytes | None` (deliberately does *not* ensure
the bucket — a missing key or bucket just means "nothing yet",
`objectstore.py:138`), `presigned_get(key, expires=1h)` (`objectstore.py:160`).

**Used by:** `BaseModelDeployment.upload_bytes` (`modeling/base.py:550`, key
`{request.id}.pt`), `BackendRequestModel.respond` for non-blocking requests
(`common/schema/request.py:153`, key `responses/{request_id}.json`), and
`GET /response/{id}` (`app.py:314`).

## PostgresProvider

`src/ndif/common/providers/postgres.py`. An async `asyncpg` pool over the user/keys
database (schema in `docker/postgres/init.sql`). A generic sink — the auth *policy*
lives in `services/api/auth.py`.

| Env var | Default | What it does |
|---|---|---|
| `NDIF_POSTGRES_URL` | `""` | **Empty disables auth entirely.** Set e.g. `postgresql://user:pass@host:5432/ndif` to enable API-key verification |
| `NDIF_POSTGRES_POOL_MIN` | `1` | Connections opened up front on first use |
| `NDIF_POSTGRES_POOL_MAX` | `10` | Pool ceiling under concurrency |
| `NDIF_POSTGRES_COMMAND_TIMEOUT_S` | `10.0` | Per-statement timeout, so a wedged DB errors instead of hanging a request |

> **This provider's default is the highest-consequence one in the system.** An
> unset `NDIF_POSTGRES_URL` disables auth *and* marks every request `trusted` —
> unsandboxed in-process execution of caller code, and `trust_remote_code` model
> loading. See [The Postgres default](#the-postgres-default-has-two-other-consequences).

`PostgresProvider.enabled` is a cheap `bool(cls.url)` with no I/O; `verify_api_key`
(`auth.py:94`) short-circuits on it. `connect()` is async and idempotent and builds
the pool under an `asyncio.Lock` with a double-check, so concurrent first requests
create exactly one pool (`postgres.py:97`). `fetch` / `fetchrow` / `fetchval` each
`await ensure()` first. Optional-dependency handling is the **opposite** of the telemetry providers:
`asyncpg` is imported in a `try/except` at module top (`postgres.py:40`) so the
module always imports, but with a URL set and the package missing, `connect()`
raises `RuntimeError` pointing at `pip install '.[postgres]'` (`postgres.py:91`).
Silently disabling auth would be a security hole; a missing metrics sink is
harmless. Same spirit at `auth.py:120`: a query failure becomes a 503, never an
unverified request. **Used by** `services/api/auth.py` only.

## InfluxProvider

`src/ndif/common/providers/influx.py`. Numeric time series for Grafana; the metric
*definitions* live in `common/metrics.py`. `NDIF_SERVICE` (`unknown`) and
`NDIF_ENVIRONMENT` (`dev`) become base tags on every point.

| Env var | Default | What it does |
|---|---|---|
| `NDIF_INFLUX_URL` | `http://localhost:8086` | InfluxDB 2.x endpoint |
| `NDIF_INFLUX_TOKEN` | `""` | Auth token |
| `NDIF_INFLUX_ORG` / `NDIF_INFLUX_BUCKET` | `ndif` / `metrics` | Target org and bucket |
| `NDIF_INFLUX_ENABLED` | `true` | `0/false/no/off` disables the provider outright |
| `NDIF_INFLUX_BATCH_SIZE` / `_FLUSH_INTERVAL_MS` / `_TIMEOUT_MS` | `500` / `1000` / `10000` | Flush when either threshold is hit; the timeout bounds each HTTP write |

`InfluxProvider.write(measurement, tags, fields)` (`influx.py:165`) is the only
method call sites use. It returns immediately if `write_api is None` (disabled,
package missing, or construction failed), drops `None` values, skips fieldless
points (InfluxDB rejects them), stamps an explicit `time_ns()` timestamp, and
enqueues — the whole body is wrapped, so a metric write never surfaces into the
caller. The timestamp matters: without one InfluxDB assigns ingestion time at
flush, and points sharing measurement+tag-set+timestamp overwrite each other
(`influx.py:188`).

Fail-open is layered: `influxdb-client` not importable → `_HAS_CLIENT` is `False`
and every `write` no-ops (`influx.py:45`, `:112`); construction failure → debug
log, provider disabled (`influx.py:137`); a failed batch flush → `_on_write_error`
logs at **debug** only, and `max_retries=0` drops the batch, keeping the buffer
bounded and `close()` non-blocking (`influx.py:131`).

**Used by:** `Metric._emit` (`common/metrics.py:58`). Connected in the API's
`post_fork`, the dispatcher's `__main__` (`dispatcher.py:393`), and each model
actor's `__init__` (`modeling/base.py:121`).

## LokiProvider

`src/ndif/common/providers/loki.py`. Ships the `ndif` logger to Grafana Loki via
`python-logging-loki`'s `LokiQueueHandler` — a `QueueHandler` whose listener thread
owns the HTTP pushes, so `emit` never blocks an event loop. `NDIF_SERVICE` /
`NDIF_ENVIRONMENT` become static stream labels.

| Env var | Default | What it does |
|---|---|---|
| `NDIF_LOKI_URL` | `""` | **Empty disables Loki**; the package is never imported. E.g. `http://loki:3100/loki/api/v1/push` |
| `NDIF_LOKI_LEVEL` | `INFO` | Minimum level shipped (the console keeps its own) |
| `NDIF_LOKI_QUEUE_MAX` | `10000` | In-memory queue bound |

`LokiProvider.connect` (`loki.py:159`) always calls `configure_console("ndif")`
first — structured console output is worth having with or without Loki, and it
stops root-handler duplication. Only then, and only with a URL set, does it build
the handler; console logging is never removed, only added to. Three wrappers around
the stock handler (`_build_handler`, `loki.py:90`): `_JsonLineFormatter` renders
each record as one structured JSON line, sharing field extraction with the console
formatter in `common/logging_setup.py`; `_LabelFilter` promotes `model_key` — the
only bounded-cardinality field — into `record.tags`, which becomes a Loki stream
label, while request/session ids stay in the line (queryable with `| json`, not
indexed); `_BoundedLokiQueueHandler` drops on `queue.Full` instead of erroring.

Fail-open, precisely: `logging_loki` not installed → `ImportError` caught, one
warning, console-only (`loki.py:185`); any other build failure → warning with
traceback, console-only (`loki.py:192`); Loki unreachable at runtime → the inner
handler's `handleError` becomes a no-op (`loki.py:124`), since stock behavior dumps
a traceback to stderr *per record*.

**Used by:** the API's `post_fork` (`gunicorn_conf.py:35`), the dispatcher's
`__main__` (`dispatcher.py:394`), the controller actor and its launcher
(`controller.py:63`, `:566`), and each model actor (`modeling/base.py:120`).

## Gotchas

> **Defaults are localhost.** Correct for `ndif start` on one host, wrong inside
> compose, where each container needs `NDIF_REDIS_URL=redis://redis:6379`,
> `NDIF_OBJECT_STORE_URL=http://minio:9000` and so on (set per service in
> `docker/docker-compose.yml`). A container that forgets them tries to reach itself.

> **The object store's two URLs.** With `NDIF_OBJECT_STORE_PUBLIC_URL` unset,
> presigned URLs are signed with the server-side host and a client outside that
> network can't resolve them. Symptom: jobs COMPLETE but the download fails.

## Related

- `docs/developing/redis-layer.md` — what the Redis clients actually carry, and
  `docs/reference/redis-keys.md` for every key, channel and stream.
- `docs/developing/adding-a-provider.md` — recipe for writing a new one.
- `docs/concepts/auth-and-limits.md` — how the Postgres provider becomes API-key auth.
