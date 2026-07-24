"""Redis provider: singleton sync/async clients for the whole process.

Three clients off one URL:
    sync_client        text mode, sync   — response pub/sub from sync code
    async_client       text mode, async  — response pub/sub from async workers
    async_bytes_client binary mode, async — the pickled request queue

Text clients use ``decode_responses=True`` (pub/sub payloads are JSON strings);
the bytes client keeps raw bytes so pickled queue values survive intact.
"""

import logging

import redis
import redis.asyncio as aioredis

from .base import Provider

logger = logging.getLogger("ndif")


class RedisProvider(Provider):
    CONFIG = {"url": ("NDIF_REDIS_URL", "redis://localhost:6379", str)}

    url: str
    sync_client: redis.Redis
    async_client: aioredis.Redis
    async_bytes_client: aioredis.Redis

    @classmethod
    def connect(cls) -> None:
        """Construct the client singletons (lazy: no socket until first command).

        ``socket_timeout=None`` is passed explicitly on purpose. redis-py 8.0+
        ships a "maintenance notifications" feature that, when the server
        advertises support (Redis 8 does), silently sets ``socket_timeout=5``
        during the handshake. That read timeout is shorter than the server-side
        block of our blocking commands (e.g. the dispatcher's
        ``brpop("queue", timeout=10)``), so the socket read would abort before
        the command returns — surfacing as spurious ``TimeoutError`` every time
        a blocking call out-waits 5s. Setting it explicitly restores the
        pre-8.0 behavior (no read timeout on blocking ops).
        """
        cls.sync_client = redis.Redis.from_url(
            cls.url, decode_responses=True, socket_timeout=None
        )
        cls.async_client = aioredis.Redis.from_url(
            cls.url, decode_responses=True, socket_timeout=None
        )
        cls.async_bytes_client = aioredis.Redis.from_url(
            cls.url, socket_timeout=None
        )

    @classmethod
    def connected(cls) -> bool:
        try:
            return bool(cls.sync_client.ping())
        except Exception:
            return False

    @classmethod
    def reset(cls) -> None:
        try:
            if getattr(cls, "sync_client", None) is not None:
                cls.sync_client.close()
        except Exception:
            pass
        # The async client is replaced on the next connect(); closing it needs
        # an event loop we may not be in here.


RedisProvider.from_env()
RedisProvider.connect()
