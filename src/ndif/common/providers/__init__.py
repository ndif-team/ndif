"""External-service providers.

Import a concrete provider from its module (e.g. ``from ndif.common.providers.redis
import RedisProvider``) — importing a module establishes that provider's
singleton, so the package keeps only the base class to avoid connecting to
services nobody asked for.
"""

from .base import Provider

__all__ = ["Provider"]
