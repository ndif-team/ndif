"""Redis provider: singleton sync/async clients for the whole process.

Three clients off one URL:
    sync_client        text mode, sync   — response pub/sub from sync code
    async_client       text mode, async  — response pub/sub from async workers
    async_bytes_client binary mode, async — the pickled request queue

Text clients use ``decode_responses=True`` (pub/sub payloads are JSON strings);
the bytes client keeps raw bytes so pickled queue values survive intact.
"""

from typing import Optional

import redis
import redis.asyncio as aioredis
from redis.backoff import ExponentialWithJitterBackoff
from redis.retry import Retry

from .base import Provider


class RedisProvider(Provider):
    CONFIG = {"url": ("NDIF_REDIS_URL", "redis://localhost:6379", str)}

    url: str
    sync_client: redis.Redis
    async_client: aioredis.Redis
    async_bytes_client: aioredis.Redis

    # Seconds an async client waits for a reply; None waits forever. Set, with
    # another ``connect()``, by a process that makes no pubsub subscription.
    async_socket_timeout: Optional[float] = None

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

        The async clients take ``async_socket_timeout``, which only the
        dispatcher sets.

        ``retry`` is explicit too: ``from_url`` doesn't get the retries that
        ``redis.Redis()`` has by default, so without it one dead connection loses
        the result of a request a model actor already finished. The async clients
        stay without it. A pubsub subscriber that reconnected silently would miss
        whatever was published meanwhile and wait forever, rather than fail.

        The async clients may hold 10,000 connections, the Redis server's default
        ``maxclients``. redis-py's own limit is 100, and every open /subscribe
        session holds one, so an API worker stopped at 98 sessions.
        """
        cls.sync_client = redis.Redis.from_url(
            cls.url,
            decode_responses=True,
            socket_timeout=None,
            retry=Retry(ExponentialWithJitterBackoff(base=1, cap=10), 3),
        )
        cls.async_client = aioredis.Redis.from_url(
            cls.url,
            decode_responses=True,
            socket_timeout=cls.async_socket_timeout,
            max_connections=10_000,
        )
        cls.async_bytes_client = aioredis.Redis.from_url(
            cls.url,
            socket_timeout=cls.async_socket_timeout,
            max_connections=10_000,
        )

    @classmethod
    def connected(cls) -> bool:
        try:
            return bool(cls.sync_client.ping())
        except Exception:
            return False


RedisProvider.from_env()
RedisProvider.connect()
