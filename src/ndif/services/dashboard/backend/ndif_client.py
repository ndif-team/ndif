"""Thin wrapper around the CLI lib functions for use from FastAPI / cron.

We collect ``on_message`` lines into a list so the HTTP response can return
the human-readable transcript alongside the structured result.
"""

from __future__ import annotations

from typing import Optional

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
    "flush_warm_cache",
    "restart",
    "status",
]


def _collect_logs() -> tuple[list[str], callable]:
    logs: list[str] = []

    def _on_message(msg: str) -> None:
        logs.append(msg)

    return logs, _on_message


def deploy(
    specs: list[dict],
    *,
    sync: bool = False,
    ray_address: Optional[str] = None,
    broker_url: Optional[str] = None,
) -> dict:
    logs, on_message = _collect_logs()
    result = _deploy(
        specs,
        sync=sync,
        ray_address=ray_address,
        broker_url=broker_url,
        on_message=on_message,
    )
    result["logs"] = logs
    return result


def evict(
    model_keys: Optional[list[str]] = None,
    checkpoints: Optional[list[tuple[str, Optional[str]]]] = None,
    *,
    ray_address: Optional[str] = None,
    broker_url: Optional[str] = None,
) -> dict:
    logs, on_message = _collect_logs()
    result = _evict(
        model_keys=model_keys,
        checkpoints=checkpoints,
        ray_address=ray_address,
        broker_url=broker_url,
        on_message=on_message,
    )
    result["logs"] = logs
    return result


def evict_all(
    *,
    ray_address: Optional[str] = None,
    broker_url: Optional[str] = None,
) -> dict:
    logs, on_message = _collect_logs()
    result = _evict(
        evict_all=True,
        ray_address=ray_address,
        broker_url=broker_url,
        on_message=on_message,
    )
    result["logs"] = logs
    return result


def flush_warm_cache(
    *,
    ray_address: Optional[str] = None,
    broker_url: Optional[str] = None,
) -> dict:
    logs, on_message = _collect_logs()
    result = _evict(
        flush_cache=True,
        ray_address=ray_address,
        broker_url=broker_url,
        on_message=on_message,
    )
    result["logs"] = logs
    return result


def status(*, ray_address: Optional[str] = None) -> dict:
    return _status(ray_address=ray_address)


def restart(
    checkpoint: Optional[str] = None,
    *,
    revision: Optional[str] = None,
    model_key: Optional[str] = None,
    ray_address: Optional[str] = None,
) -> dict:
    logs, on_message = _collect_logs()
    result = _restart(
        checkpoint or None,
        revision=revision,
        model_key=model_key,
        ray_address=ray_address,
        on_message=on_message,
    )
    result["logs"] = logs
    return result
