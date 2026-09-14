"""Postgres provider: a shared async connection pool over the user/keys DB.

NDIF's user data — users, API keys, and the tags they grant — lives in a Postgres
database (schema in ``init.sql``: ``users`` / ``user_tags`` / ``keys`` /
``key_user_tag_assignments``).
This provider is the thin, generic sink the API's auth layer
(:mod:`ndif.services.api.auth`) queries through; the auth *logic* (what counts
as a valid key, which HTTP status a failure maps to) lives there, not here.

Design mirrors the other optional providers (Loki / InfluxDB):

  * **Opt-in** — nothing connects unless ``NDIF_POSTGRES_URL`` is set. An install
    that never points at a Postgres (and may not even have ``asyncpg``) pays and
    imports nothing; :meth:`enabled` returns ``False`` and callers skip auth.
  * **Optional dependency** — ``asyncpg`` is imported lazily. If the URL *is* set
    but the package is missing, :meth:`connect` raises loudly rather than
    silently disabling auth (a silent "auth off" would be a security hole — the
    opposite of the fail-open telemetry providers, whose absence is harmless).

Unlike the sync providers, the connection is an ``asyncpg`` pool, which can only
be created inside a running event loop. So this provider does **not** connect at
import (there's no loop yet in a fresh worker) — only :meth:`from_env` runs. The
pool is created lazily on first use via :meth:`ensure`, guarded by a lock so
concurrent first requests build exactly one pool. Every query classmethod
acquires a connection from the pool and returns it, so callers never manage
connections directly.
"""

import asyncio
import logging
from typing import Any, ClassVar, List, Optional

from .base import Provider

logger = logging.getLogger("ndif")

# asyncpg is an optional import: only an install that sets NDIF_POSTGRES_URL
# needs it. A checkout without it should still import this module (enabled()
# stays usable); the missing dependency only bites at connect() time, and only
# when a URL is actually configured.
try:
    import asyncpg

    _HAS_ASYNCPG = True
except Exception:  # pragma: no cover - exercised only where the dep is absent
    asyncpg = None  # type: ignore[assignment]
    _HAS_ASYNCPG = False


class PostgresProvider(Provider):
    """Owns (once per process) an async connection pool to the user/keys DB."""

    CONFIG = {
        # Empty (the default) disables Postgres entirely — asyncpg is never used
        # and auth is a no-op. Set it (e.g.
        # postgresql://user:pass@host:5432/keys) to enable API-key verification.
        "url": ("NDIF_POSTGRES_URL", "", str),
        # Pool sizing. min_size connections are opened up front (on first use),
        # up to max_size under concurrency.
        "min_size": ("NDIF_POSTGRES_POOL_MIN", 1, int),
        "max_size": ("NDIF_POSTGRES_POOL_MAX", 10, int),
        # Per-query timeout (seconds): bounds how long a single statement may run
        # so a wedged DB surfaces as an error instead of hanging a request.
        "command_timeout_s": ("NDIF_POSTGRES_COMMAND_TIMEOUT_S", 10.0, float),
    }

    url: str
    min_size: int
    max_size: int
    command_timeout_s: float

    pool: ClassVar[Optional[Any]] = None
    # Guards pool creation so concurrent first requests build exactly one pool.
    # asyncio.Lock binds to the running loop lazily (on first await), so
    # constructing it at class-definition time — before any loop exists — is fine.
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    def enabled(cls) -> bool:
        """Whether Postgres is configured (a URL is set). Cheap, no I/O."""
        return bool(cls.url)

    @classmethod
    async def connect(cls) -> None:
        """Create the connection pool. Idempotent; must run inside an event loop.

        Raises if the URL is set but ``asyncpg`` isn't installed — auth being
        silently disabled would be worse than a loud failure.
        """
        if not cls.enabled() or cls.pool is not None:
            return
        if not _HAS_ASYNCPG:
            raise RuntimeError(
                "NDIF_POSTGRES_URL is set but asyncpg is not installed; "
                "install the `postgres` extra (pip install '.[postgres]')."
            )

        async with cls._lock:
            # Double-check under the lock: another coroutine may have built it
            # while we waited.
            if cls.pool is not None:
                return
            cls.pool = await asyncpg.create_pool(
                dsn=cls.url,
                min_size=cls.min_size,
                max_size=cls.max_size,
                command_timeout=cls.command_timeout_s,
            )
            logger.info(
                "Postgres connected",
                extra={"postgres_pool_max": cls.max_size},
            )

    @classmethod
    async def ensure(cls) -> None:
        """Connect if the pool isn't up yet (lazy first-use entry point)."""
        if cls.pool is None:
            await cls.connect()

    @classmethod
    async def fetch(cls, query: str, *args: Any) -> List[Any]:
        """Run a query and return all rows (list of ``asyncpg.Record``)."""
        await cls.ensure()
        async with cls.pool.acquire() as conn:
            return await conn.fetch(query, *args)

    @classmethod
    async def fetchrow(cls, query: str, *args: Any) -> Optional[Any]:
        """Run a query and return the first row, or ``None`` if there are none."""
        await cls.ensure()
        async with cls.pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    @classmethod
    async def fetchval(cls, query: str, *args: Any) -> Any:
        """Run a query and return the first column of the first row (or ``None``)."""
        await cls.ensure()
        async with cls.pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    @classmethod
    def connected(cls) -> bool:
        return cls.pool is not None

    @classmethod
    async def disconnect(cls) -> None:
        """Close the pool. Idempotent."""
        if cls.pool is None:
            return
        try:
            await cls.pool.close()
        except Exception:
            pass
        cls.pool = None


# Load config at import (cheap, no I/O). The pool is created lazily on first use
# because asyncpg needs a running event loop, which doesn't exist yet here.
PostgresProvider.from_env()
