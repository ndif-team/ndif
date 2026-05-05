"""Shared helpers used by the per-command lib modules (``deploy``, ``evict``,
``restart``, ``status``).

Anything in here is private to ``cli/lib/`` — callers should import from the
specific command module (e.g. ``from ndif.cli.lib.deploy import deploy``).
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from .checks import check_ray, check_redis


logger = logging.getLogger(__name__)

OnMessage = Optional[Callable[[str], None]]


class NDIFConnectivityError(RuntimeError):
    """Raised when prerequisite services (Redis broker / Ray) are unreachable."""


def emit(on_message: OnMessage, msg: str) -> None:
    if on_message is not None:
        on_message(msg)


def check_prereqs(broker_url: Optional[str], ray_address: Optional[str]) -> None:
    if broker_url and not check_redis(broker_url):
        raise NDIFConnectivityError(f"Cannot reach broker at {broker_url}")
    if ray_address and not check_ray(ray_address):
        raise NDIFConnectivityError(f"Cannot reach Ray at {ray_address}")


def ensure_ray_connected(ray_address: Optional[str] = None) -> None:
    """Ensure the Ray client is connected. Reset + reconnect once on failure.

    Delegates the actual liveness check to ``RayProvider.connected()`` —
    which already covers ``ray.is_initialized()``, the TCP listen probe,
    and a controller-handle round-trip.
    """
    # Lazy import — avoids dragging the broker stack into nnsight CLI startup.
    from ...common.providers.ray import RayProvider

    if ray_address:
        RayProvider.ray_url = ray_address

    try:
        if RayProvider.connected():
            return
    except Exception:
        pass

    RayProvider.reset()
    try:
        RayProvider.connect()
    except Exception as e:
        raise NDIFConnectivityError(
            f"Cannot connect to Ray at {RayProvider.ray_url}: {e}"
        ) from e


def normalize_specs(specs: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for spec in specs:
        if "checkpoint" not in spec:
            raise ValueError(f"Model spec missing 'checkpoint': {spec}")
        out.append(
            {
                "checkpoint": spec["checkpoint"],
                "revision": spec.get("revision"),
                "pinned": bool(spec.get("pinned", False)),
                "actor_class": spec.get("actor_class"),
                "padding_factor": spec.get("padding_factor"),
                "execution_timeout_seconds": spec.get("execution_timeout_seconds"),
            }
        )
    return out
