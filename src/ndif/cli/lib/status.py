"""Programmatic implementation of cluster status fetch (read-only).

Note: there is no ``ndif status`` Click command that uses this — the CLI's
``status`` command exists separately under ``cli/commands/status.py`` and
hits the API's HTTP ``/status`` endpoint. This lib function is what the
dashboard backend's ``ndif_client.py`` calls when it needs the controller
payload directly through Ray.
"""

from __future__ import annotations

from typing import Optional

from ._common import NDIFConnectivityError, ensure_ray_connected
from .session import get_env
from .util import get_controller_actor_handle


def status(*, ray_address: Optional[str] = None) -> dict:
    """Return the controller's full status dict."""
    import ray

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    ensure_ray_connected(ray_address)
    controller = get_controller_actor_handle()
    return ray.get(controller.status.remote())


__all__ = ["status", "NDIFConnectivityError"]
