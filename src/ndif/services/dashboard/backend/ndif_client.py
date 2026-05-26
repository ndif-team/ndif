"""Thin wrapper around the CLI lib functions for use from FastAPI / cron.

The lib functions accept an ``on_message`` callback for streaming progress
lines (matches what the CLI's ``click.echo`` does). Here we collect those
lines into ``result["logs"]`` so HTTP responses carry the human-readable
transcript alongside the structured result.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

from ....cli.lib._common import NDIFConnectivityError
from ....cli.lib.deploy import deploy as _deploy
from ....cli.lib.evict import evict as _evict
from ....cli.lib.restart import restart as _restart
from ....cli.lib.status import status as _status


__all__ = [
    "NDIFConnectivityError",
    "deploy",
    "evict",
    "evict_all",
    "restart",
    "status",
]


def _with_logs(fn: Callable) -> Callable:
    """Tee the lib function's ``on_message`` callback into ``result["logs"]``."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        logs: list[str] = []
        kwargs["on_message"] = logs.append
        result = fn(*args, **kwargs)
        if isinstance(result, dict):
            result["logs"] = logs
        return result

    return wrapper


# CLI lib functions that accept ``on_message`` — wrap them once.
deploy = _with_logs(_deploy)
evict = _with_logs(_evict)
restart = _with_logs(_restart)

# Status is a cheap read with no progress to stream.
status = _status


def evict_all(**kwargs) -> dict:
    return evict(evict_all=True, **kwargs)
