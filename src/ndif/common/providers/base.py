"""Provider base: a uniform interface to an external service.

A *Provider* wraps a service this codebase connects to and interacts with —
Redis, Ray, an object store, telemetry, etc. Each provider:

  * declares the env vars that configure it (``CONFIG``), which the base turns
    into ``from_env`` / ``to_env`` automatically;
  * owns its singleton connection handle(s) as class attributes;
  * exposes ``connect`` / ``connected`` / ``reset`` / ``disconnect``.

Providers are classmethod singletons: one connection per process, held on the
class and established once at import (``from_env(); connect()``). There's no
instance state — call ``RedisProvider.sync_client`` etc. directly.
"""

import logging
import os
from typing import Any, Callable, ClassVar, Dict, Tuple

logger = logging.getLogger("ndif")

# attr name -> (ENV_VAR, typed default, caster applied to the env string)
ConfigSpec = Dict[str, Tuple[str, Any, Callable[[str], Any]]]


class Provider:
    """Base class for external-service connection managers.

    Subclasses set :attr:`CONFIG` to declare their environment-driven settings
    and override :meth:`connect` / :meth:`connected` (and optionally
    :meth:`reset`). Settings load into class attributes named by ``CONFIG``'s
    keys; e.g. ``{"url": ("NDIF_REDIS_URL", "redis://...", str)}`` populates
    ``cls.url``.
    """

    CONFIG: ClassVar[ConfigSpec] = {}

    @classmethod
    def from_env(cls) -> None:
        """Load configuration from the environment into class attributes."""
        for attr, (var, default, cast) in cls.CONFIG.items():
            raw = os.environ.get(var)
            setattr(cls, attr, cast(raw) if raw is not None else default)

    @classmethod
    def to_env(cls) -> Dict[str, str]:
        """Export this provider's configuration as an env-var dict.

        Useful for propagating config into a subprocess / worker environment.
        """
        return {var: str(getattr(cls, attr)) for attr, (var, _, _) in cls.CONFIG.items()}

    @classmethod
    def connect(cls) -> None:
        """Establish the singleton connection handle(s). No-op by default."""

    @classmethod
    def disconnect(cls) -> None:
        """Tear down the connection handle(s). No-op by default."""

    @classmethod
    def reset(cls) -> None:
        """Drop any half-open connection state before a reconnect."""

    @classmethod
    def connected(cls) -> bool:
        """Whether the service is reachable right now. ``True`` by default."""
        return True
