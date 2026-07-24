---
title: Adding a Provider
one_liner: Recipe for wiring a new backing service into NDIF — the Provider base-class contract, the env-var tuple convention, and the fail-open discipline every optional provider must follow.
tags: [internals, dev, telemetry, redis]
related: [docs/developing/providers.md, docs/developing/repo-layout.md, docs/developing/adding-a-service.md, docs/developing/telemetry-internals.md, docs/developing/redis-layer.md, docs/reference/env-vars.md, docs/developing/contributing.md, docs/operating/configuration.md]
sources: [src/ndif/common/providers/base.py, src/ndif/common/providers/influx.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/objectstore.py, src/ndif/common/providers/postgres.py, src/ndif/common/providers/redis.py, src/ndif/common/providers/ray.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, pyproject.toml]
---

# Adding a Provider

## What this covers

A **provider** is NDIF's uniform wrapper around an external service it connects to
— Redis, Ray, an S3-compatible object store, Postgres, Loki, InfluxDB. This page
is the recipe for adding one: a different object store, a second metrics sink, a
message bus, whatever. It's short because the base class is small; the
interesting part is the discipline around failure.

Two constraints shape the pattern:

1. **NDIF runs in many processes it doesn't control** — gunicorn workers, a spawned
   dispatcher, a detached Ray controller actor, one Ray actor per model replica, a
   runner subprocess per untrusted request. None share memory. So a provider is a
   **classmethod singleton on the class**, configured from the environment and
   established once per process at import, never an object passed around.
2. **Most backing services are optional, and their absence must be survivable.**
   The self-hosting story is "runs end to end with zero configuration, and each
   optional URL turns one thing on". A provider therefore has to survive *two*
   independent failures: its Python package not being installed, and its server
   not being reachable.

## The base-class contract

`src/ndif/common/providers/base.py` is 76 lines. The whole surface:

```python
# attr name -> (ENV_VAR, typed default, caster applied to the env string)
ConfigSpec = Dict[str, Tuple[str, Any, Callable[[str], Any]]]
```

| Classmethod | Does |
|---|---|
| `from_env()` | `CONFIG` → class attributes. Inherited; don't override. |
| `to_env()` | Class attributes → env dict, for propagating into a subprocess or Ray worker. Inherited. |
| `connect()` | Build the singleton handle(s). No-op by default — **override**. |
| `connected()` | Reachable right now? `True` by default — **override**. |
| `reset()` | Drop half-open state before a reconnect. Override if reconnecting needs cleanup. |
| `disconnect()` | Tear the handle(s) down. Override if there's anything to close. |

**`from_env` / `to_env` are generated from `CONFIG`** (`base.py:39` and `base.py:46`),
which is why the tuple shape matters. `to_env` is not decoration: the controller
uses it to propagate provider config into a Ray actor's `runtime_env`
(`.../cluster/deployment.py:16`), because Ray workers inherit only the node's
ambient environment. A provider a model actor needs must round-trip through
`to_env` correctly, or the actor will connect to the wrong place — or to nothing.

## The env-var tuple convention

```python
class MyProvider(Provider):
    CONFIG = {
        # Empty (the default) disables this provider entirely.
        "url": ("NDIF_MYTHING_URL", "", str),
        "timeout_s": ("NDIF_MYTHING_TIMEOUT_S", 10.0, float),
        "enabled": ("NDIF_MYTHING_ENABLED", True, _boolish),
    }

    url: str
    timeout_s: float
    enabled: bool
```

Rules the existing providers all follow:

- **Key = attribute name.** `"url"` populates `cls.url`.
- **Prefix `NDIF_`, grouped by subsystem.** `NDIF_MYTHING_*`.
- **The default is already typed** (`10.0`, not `"10.0"`); the caster applies only
  when the env var is present (`base.py:43`).
- **Declare the attribute annotations under `CONFIG`.** They're documentation for
  readers and type checkers — `from_env` sets them at runtime.
- **Comment each entry with what it does and what empty means.** The object store's
  `CONFIG` (`objectstore.py:38`) is the model: seven entries, each with the reason
  it exists, including why `region` is set explicitly (so presigning never
  round-trips to discover it over an endpoint the server can't reach).
- **Empty string means off** for an optional subsystem. Add a separate `_ENABLED`
  boolean only when you need to disable something that *is* configured;
  `NDIF_INFLUX_ENABLED` is the sole such case.
- **Booleans go through a `_boolish` caster** — `"1"/"true"/"yes"/"on"` — rather
  than `bool`, which would make `"false"` truthy.

## Lazy connection and singleton behavior

Every provider module ends with the same two lines:

```python
MyProvider.from_env()
MyProvider.connect()
```

so **importing the module establishes the singleton**. That's why
`providers/__init__.py` exports only the base class: it would otherwise connect to
services nobody asked for, at import of anything under `common/`.

`connect()` must be **cheap and idempotent**. Cheap: `RedisProvider.connect`
(`redis.py:31`) and `ObjectStoreProvider.connect` (`objectstore.py:90`) construct
client objects that open no socket until the first call. Idempotent: every process
entry point — each uvicorn worker, each Ray actor — calls it without coordinating,
and it must be a no-op the second time (`influx.py:112` returns early if
`cls.client is not None`).

Two shapes deviate, both for good reasons you may need to copy:

- **Postgres does not connect at import** (`postgres.py:158` runs only
  `from_env()`). An `asyncpg` pool can only be built inside a running event loop,
  which doesn't exist yet in a fresh worker. It connects lazily on first use via
  `ensure()`, guarded by an `asyncio.Lock` so concurrent first requests build
  exactly one pool.
- **Loki and Influx own background threads**, and threads don't survive `fork()`.
  Any process that forks children which will emit must not import these before
  forking; see the `post_fork` hook in `services/api/gunicorn_conf.py:35`. If your
  provider spawns a thread, document this in the `connect` docstring the way both
  of those do.

`connected()` should be a real reachability probe, not a "did we build an object"
check — `RedisProvider.connected` pings, `ObjectStoreProvider.connected` calls
`list_buckets`. `PostgresProvider.ensure` (`postgres.py:115`) builds on it to
reconnect lazily when the pool isn't up.

## Fail-open discipline

This is the part to get right. An optional provider has to survive **two**
independent absences.

**(a) The package isn't installed.** Guard the import at module level and record a
flag:

```python
try:
    from influxdb_client import InfluxDBClient, Point, WritePrecision
    _HAS_CLIENT = True
except Exception:  # pragma: no cover - exercised only where the dep is absent
    InfluxDBClient = None  # type: ignore[assignment]
    _HAS_CLIENT = False
```

`connect` then returns early when `not _HAS_CLIENT` (`influx.py:112`) and every
write path is a no-op. **The module must still import.** A checkout without the
`metrics` extra imports `ndif.common.metrics` — which imports the Influx provider
— on every service; a hard `ImportError` there would take the whole server down
over missing telemetry.

Loki takes the stricter, lazier variant: `logging_loki` is imported *inside*
`_build_handler` (`loki.py:97`), which is only reached when `NDIF_LOKI_URL` is set,
and `connect` catches the `ImportError` and warns once (`loki.py:185`). An install
that never points at a Loki imports nothing and pays nothing.

**(b) The server is unreachable, down, or flapping.** Every call path is wrapped
and degrades to a no-op:

- Construction failures are swallowed (`influx.py:137`) — metrics stay disabled and
  the service runs.
- Per-write failures never reach the caller (`influx.py:201`).
- Errors are logged at **debug**, not info or warning: a flapping server would
  otherwise spam a line per flush (`influx.py:157`).
- Backpressure is bounded. The Loki handler drops on a full queue instead of
  raising (`loki.py:103`), and the Influx writer sets `max_retries=0` so a failed
  batch is dropped rather than blocking `close()` and growing the buffer without
  limit (`influx.py:131`).
- Noisy library error handling is silenced: `python-logging-loki`'s inner handler
  would dump a full traceback to stderr per record, so it's neutered
  (`loki.py:122`).

### When *not* to fail open

Fail-open is a property of **best-effort telemetry**, not a house rule. Two
counter-examples in the tree:

**Postgres fails loud, deliberately.** If `NDIF_POSTGRES_URL` is set but `asyncpg`
is missing, `connect` raises (`postgres.py:91`) — because silently disabling auth
is a security hole, the exact opposite of a dropped metric. And if the DB is
configured but unreachable, `verify_api_key` returns **503** rather than letting
the request through (`services/api/auth.py:126`): it fails *closed*.

**Redis and the object store don't fail open at all.** They're on the critical
path — no Redis means no queue, no object store means no results. A silent no-op
there would turn a loud outage into requests that vanish.

The test to apply: *if this call silently did nothing, would the system be
degraded-but-correct, or quietly wrong?* Degraded-but-correct → fail open. Quietly
wrong → raise.

## Add the extra

An optional provider's package belongs in a `[project.optional-dependencies]`
group, with a comment saying what the group turns on:

```toml
[project.optional-dependencies]
# Telemetry sinks — optional. Both providers fail open when their package is
# absent (or their server is unreachable) ...
metrics = [
    "influxdb-client",
    "python-logging-loki",
]
```

Then, if it should be in the image: add the group to the Dockerfile's install
list (`docker/Dockerfile:45`) and **pin the package in `requirements.txt`** — the
Dockerfile installs with `--no-deps`, so extras only declare intent;
`requirements.txt` is what actually installs.

Finally, document each new var in the README's `NDIF_*` table and in
[docs/reference/env-vars.md](../reference/env-vars.md).

## A minimal end-to-end example

A second metrics sink — a StatsD-style UDP emitter — with both failure modes
handled:

```python
"""StatsD provider: ship NDIF counters to a StatsD daemon, fail-open.

A cheap alternative sink to :mod:`~ndif.common.providers.influx` for
deployments that already run a StatsD/Datadog agent. Nothing happens unless
NDIF_STATSD_URL is set; if the ``statsd`` package is missing or the daemon is
down, every emit is a no-op and the service runs unchanged.
"""

import logging
from typing import ClassVar, Optional
from urllib.parse import urlparse

from .base import Provider

logger = logging.getLogger("ndif")

try:
    import statsd
    _HAS_STATSD = True
except Exception:  # pragma: no cover - exercised only where the dep is absent
    statsd = None  # type: ignore[assignment]
    _HAS_STATSD = False


class StatsdProvider(Provider):
    CONFIG = {
        # Empty (the default) disables StatsD entirely.
        "url": ("NDIF_STATSD_URL", "", str),
        # Prefixed onto every metric name so several NDIFs can share a daemon.
        "prefix": ("NDIF_STATSD_PREFIX", "ndif", str),
        "service": ("NDIF_SERVICE", "unknown", str),
    }

    url: str
    prefix: str
    service: str

    client: ClassVar[Optional["statsd.StatsClient"]] = None

    @classmethod
    def connect(cls) -> None:
        """Build the UDP client. Idempotent, fail-open (UDP opens no socket)."""
        if not cls.url or not _HAS_STATSD or cls.client is not None:
            return
        parsed = urlparse(cls.url)
        try:
            cls.client = statsd.StatsClient(
                parsed.hostname, parsed.port or 8125,
                prefix=f"{cls.prefix}.{cls.service}",
            )
        except Exception:
            logger.debug("StatsD telemetry unavailable", exc_info=True)

    @classmethod
    def connected(cls) -> bool:
        return cls.client is not None

    @classmethod
    def incr(cls, name: str, count: int = 1) -> None:
        """Increment a counter (non-blocking, fail-open)."""
        if cls.client is None:
            return
        try:
            cls.client.incr(name, count)
        except Exception:
            # Never let a metric emit surface into the caller.
            logger.debug("StatsD emit failed", exc_info=True)


StatsdProvider.from_env()
StatsdProvider.connect()
```

Call sites just import it — the import connects it —  and call
`StatsdProvider.incr("requests.received")`.

## Checklist

- [ ] `src/ndif/common/providers/<name>.py` with a module docstring stating the design constraint (opt-in? fail-open? fork-sensitive?) before the mechanism.
- [ ] `CONFIG` with `NDIF_`-prefixed vars, typed defaults, and a comment per entry.
- [ ] `connect` cheap and idempotent; `connected` a real reachability probe.
- [ ] Optional package guarded at import (or imported lazily) so the module always imports.
- [ ] Every call path wrapped; failures logged at **debug**; queues/retries bounded — *or* a written reason why this one must raise.
- [ ] `from_env(); connect()` at the bottom of the module; nothing added to `providers/__init__.py`.
- [ ] A `[project.optional-dependencies]` extra, added to the Dockerfile install list and pinned in `requirements.txt`.
- [ ] `to_env()` round-trips everything a Ray actor would need, if a model actor uses it.
- [ ] Env vars in the README table and [env-vars.md](../reference/env-vars.md).

## Related

- [providers.md](./providers.md) — reference for each existing provider.
- [telemetry-internals.md](./telemetry-internals.md) — how the Loki and Influx providers plug into logging and metrics.
- [redis-layer.md](./redis-layer.md) — what's built on top of `RedisProvider`.
- [adding-a-service.md](./adding-a-service.md) — the other half: wiring a service that uses your provider.
- [docs/operating/configuration.md](../operating/configuration.md) — how env-only configuration layers in practice.
