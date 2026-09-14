"""Redis key / stream / channel constants for cross-service coordination.

Grouped here because the API workers and the queue dispatcher must agree on the
exact Redis keys they coordinate through. Each submodule documents one
mechanism (the coalesced ``/status`` and ``/env`` caches); import the constants
from this package.

Note: this is the *keys* package. The Redis connection lives in
:mod:`ndif.common.providers.redis`.
"""

from .env import (
    ENV_KEY,
    ENV_READY_CHANNEL,
    ENV_REQUESTED_KEY,
    ENV_TIMEOUT_S,
    ENV_TRIGGER_STREAM,
    ENV_TTL_S,
)
from .events import (
    EVENT_KILL,
    EVENT_QUEUE_STATE,
    EVENT_RECONCILE,
    EVENT_RESPONSE_TTL_S,
    EVENTS_STREAM,
)
from .status import (
    STATUS_KEY,
    STATUS_READY_CHANNEL,
    STATUS_REQUESTED_KEY,
    STATUS_TIMEOUT_S,
    STATUS_TRIGGER_STREAM,
    STATUS_TTL_S,
)

__all__ = [
    "ENV_KEY",
    "ENV_READY_CHANNEL",
    "ENV_REQUESTED_KEY",
    "ENV_TIMEOUT_S",
    "ENV_TRIGGER_STREAM",
    "ENV_TTL_S",
    "EVENTS_STREAM",
    "EVENT_KILL",
    "EVENT_QUEUE_STATE",
    "EVENT_RECONCILE",
    "EVENT_RESPONSE_TTL_S",
    "STATUS_KEY",
    "STATUS_READY_CHANNEL",
    "STATUS_REQUESTED_KEY",
    "STATUS_TIMEOUT_S",
    "STATUS_TRIGGER_STREAM",
    "STATUS_TTL_S",
]
