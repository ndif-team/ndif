"""Redis provider for both sync and async Redis client access.

This module provides a centralized Redis connection manager that supports
both synchronous and asynchronous clients, following the Provider pattern.

Example:
    >>> from .providers.redis import RedisProvider
    >>> # Async usage
    >>> await RedisProvider.async_client.get("key")
    >>> # Sync usage
    >>> RedisProvider.sync_client.get("key")
"""

import os
import logging

import redis
import redis.asyncio

from . import Provider

logger = logging.getLogger("ndif")


class RedisProvider(Provider):
    """Provider for Redis connections supporting both sync and async clients.

    Maintains two Redis client instances:
        - sync_client: For use in synchronous contexts
        - async_client: For use in async contexts

    Both clients connect to the same Redis instance configured via
    the NDIF_BROKER_URL environment variable.

    Attributes:
        broker_url: Redis connection URL.
        sync_client: Synchronous Redis client.
        async_client: Asynchronous Redis client.
    """

    broker_url: str
    sync_client: redis.Redis
    async_client: redis.asyncio.Redis

    @classmethod
    def from_env(cls) -> None:
        """Load Redis configuration from environment variables."""
        super().from_env()
        cls.broker_url = os.environ.get("NDIF_BROKER_URL", "redis://localhost:6379")

    @classmethod
    def to_env(cls) -> dict:
        """Export Redis configuration to environment variables."""
        return {
            **super().to_env(),
            "NDIF_BROKER_URL": cls.broker_url,
        }

    @classmethod
    def connect(cls) -> None:
        """Establish both sync and async Redis connections."""
        logger.info(f"Connecting to Redis at {cls.broker_url}...")
        cls.sync_client = redis.Redis.from_url(cls.broker_url)
        cls.async_client = redis.asyncio.Redis.from_url(cls.broker_url)
        logger.info("Connected to Redis")

    @classmethod
    def connected(cls) -> bool:
        """Check if Redis is connected by pinging the server."""
        try:
            return cls.sync_client.ping()
        except Exception:
            return False

    @classmethod
    def reset(cls) -> None:
        """Close existing connections."""
        try:
            if hasattr(cls, "sync_client") and cls.sync_client:
                cls.sync_client.close()
        except Exception:
            pass
        try:
            if hasattr(cls, "async_client") and cls.async_client:
                # Note: async close should be awaited, but we're in sync context
                # The connection will be replaced on next connect() anyway
                pass
        except Exception:
            pass


# Initialize from environment on module load
RedisProvider.from_env()
RedisProvider.connect()
