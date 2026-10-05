"""Redis provider: singleton sync/async clients for the whole process.

Three clients off one URL:
    sync_client        text mode, sync   — response pub/sub from sync code
    async_client       text mode, async  — response pub/sub from async workers
    async_bytes_client binary mode, async — the pickled request queue

Text clients use ``decode_responses=True`` (pub/sub payloads are JSON strings);
the bytes client keeps raw bytes so pickled queue values survive intact.
"""

import logging
from typing import Optional, Tuple

import redis
import redis.asyncio as aioredis

from .base import Provider

logger = logging.getLogger("ndif")

# Fallbacks for max_publish_bytes, when the server's own limits can't decide.
#
# STOCK_PUBSUB_SOFT_LIMIT_BYTES is Redis's out-of-the-box pubsub soft limit
# (`client-output-buffer-limit pubsub 32mb 8mb 60`). It is the conservative
# answer when `CONFIG GET` is blocked or renamed (managed Redis), because a
# server we can't inspect might be running stock limits — and losing responses
# is the direction to guard against.
#
# UNLIMITED_PUBSUB_CEILING_BYTES applies only when the server reports *no*
# output-buffer limits at all (hard and soft both 0). No limit doesn't make an
# arbitrarily large pubsub message a good idea — it still buffers in server
# memory per subscriber — so keep a sane ceiling rather than going uncapped.
STOCK_PUBSUB_SOFT_LIMIT_BYTES = 8 * 1024 * 1024
UNLIMITED_PUBSUB_CEILING_BYTES = 20 * 1024 * 1024


class RedisProvider(Provider):
    CONFIG = {"url": ("NDIF_REDIS_URL", "redis://localhost:6379", str)}

    url: str
    sync_client: redis.Redis
    async_client: aioredis.Redis
    async_bytes_client: aioredis.Redis

    # Memoized max_publish_bytes, per process (None = not derived yet).
    _max_publish_bytes: Optional[int] = None

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
        # A new connection may be a different server with different limits.
        cls._max_publish_bytes = None

    @classmethod
    def max_publish_bytes(cls) -> int:
        """The largest payload safely published as one pubsub message, in bytes.

        A pubsub subscriber whose output buffer exceeds the server's
        ``client-output-buffer-limit pubsub`` is *disconnected*, and every
        message buffered for it — the one that tipped it over included — is
        gone. For the inline result path that means the job is COMPLETED, the
        websocket closes, and the result is unrecoverable. So the cap on what
        may ride a channel has to come from the Redis it rides through: this
        derives it from the server's own limits (``CONFIG GET``), and an
        operator who wants bigger inline results raises their Redis pubsub
        limits and the cap follows.

        The derivation is ``min(soft, hard // 2)``, each bound counted only
        when configured (0 disables a limit in Redis). The soft limit is the
        primary bound on purpose: Redis's omem accounting carries an
        allocator-dependent overhead factor (measured 1.15x on one box, 1.67x
        on another — where a 20.04 MB message produced omem 33,554,456, past
        the stock 32 MiB hard kill), so a cap keyed to the hard limit can be
        eaten by overhead alone. Against stock redis:7 (32 MiB hard, 8 MiB
        soft) the cap is 8 MiB, whose ~1.67x omem can never reach either
        limit. With no limits configured at all, a fixed 20 MiB ceiling; when
        ``CONFIG GET`` fails for any reason (blocked or renamed on managed
        Redis), a conservative fixed 8 MiB — never uncapped, never an error.

        Memoized per process: every process reaches one Redis through this
        provider, and the limits don't move under a running server.
        """
        if cls._max_publish_bytes is None:
            cls._max_publish_bytes = cls._derive_max_publish_bytes()
        return cls._max_publish_bytes

    @classmethod
    def _derive_max_publish_bytes(cls) -> int:
        try:
            reply = cls.sync_client.config_get("client-output-buffer-limit")
            raw = reply["client-output-buffer-limit"]
            if isinstance(raw, bytes):
                raw = raw.decode()
            hard, soft = cls._pubsub_limits(raw)
        except Exception as error:
            logger.warning(
                "Could not read the server's client-output-buffer-limit "
                "(%s: %s); capping pubsub payloads at the stock soft limit "
                "of %d bytes.",
                type(error).__name__,
                error,
                STOCK_PUBSUB_SOFT_LIMIT_BYTES,
            )
            return STOCK_PUBSUB_SOFT_LIMIT_BYTES
        bounds = []
        if soft > 0:
            bounds.append(soft)
        if hard > 0:
            bounds.append(hard // 2)
        return min(bounds) if bounds else UNLIMITED_PUBSUB_CEILING_BYTES

    @staticmethod
    def _pubsub_limits(raw: str) -> Tuple[int, int]:
        """The (hard, soft) byte limits of the ``pubsub`` client class.

        ``CONFIG GET client-output-buffer-limit`` answers one flat string of
        ``<class> <hard> <soft> <seconds>`` groups, e.g.
        ``normal 0 0 0 slave 268435456 67108864 60 pubsub 33554432 8388608 60``.
        Raises (for the caller's fallback) when the pubsub group is missing or
        unreadable.
        """
        fields = raw.split()
        at = fields.index("pubsub")
        return int(fields[at + 1]), int(fields[at + 2])

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
