"""Client side of the dispatcher's operational event stream.

``ndif queue`` / ``ndif kill`` send a request event carrying a unique
``response_key`` and block for the dispatcher's JSON reply; ``notify_reconcile``
fire-and-forgets a reconcile event after a deploy/evict. See
:mod:`ndif.common.redis.events` for the protocol.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterable, Optional

from .. import config
from ...common.redis import (
    EVENT_KILL,
    EVENT_QUEUE_STATE,
    EVENT_RECONCILE,
    EVENTS_STREAM,
)


def _client(redis_url: str):
    import redis

    # socket_timeout=None: redis-py 8.0+ silently sets socket_timeout=5 against
    # Redis 8 servers, which would abort a blocking brpop before it returns.
    return redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=None)


def _request(event_type: str, redis_url: Optional[str], timeout: int, **fields) -> dict:
    """Send a request event and block for the dispatcher's JSON reply."""
    client = _client(redis_url or config.get("NDIF_REDIS_URL"))
    try:
        response_key = f"ndif:cli:{event_type}:{os.getpid()}:{time.time_ns()}"
        client.xadd(EVENTS_STREAM, {"event_type": event_type,
                                    "response_key": response_key, **fields},
                    maxlen=64, approximate=True)
        result = client.brpop(response_key, timeout=timeout)
        if result is None:
            raise TimeoutError(
                f"No response from the dispatcher for {event_type}. Is the API running?"
            )
        return json.loads(result[1])
    finally:
        client.close()


def fetch_queue_state(redis_url: Optional[str] = None, timeout: int = 5) -> dict:
    return _request(EVENT_QUEUE_STATE, redis_url, timeout)


def kill_request(request_id: str, redis_url: Optional[str] = None, timeout: int = 5) -> dict:
    return _request(EVENT_KILL, redis_url, timeout, request_id=request_id)


def notify_reconcile(redis_url: Optional[str], model_keys: Iterable[str]) -> None:
    """Fire-and-forget reconcile events for the given models. Best-effort.

    A Redis hiccup here must not fail the deploy/evict that already succeeded on
    the controller, so errors are swallowed.
    """
    keys = sorted({mk for mk in model_keys if mk})
    if not keys:
        return
    try:
        client = _client(redis_url or config.get("NDIF_REDIS_URL"))
        try:
            for mk in keys:
                client.xadd(EVENTS_STREAM, {"event_type": EVENT_RECONCILE, "model_key": mk},
                            maxlen=64, approximate=True)
        finally:
            client.close()
    except Exception:
        pass
