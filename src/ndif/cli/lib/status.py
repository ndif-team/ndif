"""Programmatic cluster-status fetch (read-only).

Returns the controller's status payload directly through Ray. The ``ndif
status`` command renders its own fetch inline; this lib function exists for
non-CLI callers (the dashboard backend) that want the raw dict.
"""

from __future__ import annotations

from typing import Optional

from .. import config
from ._common import NDIFConnectivityError, ensure_ray_connected


def status(*, ray_address: Optional[str] = None) -> dict:
    """Return the controller's full status dict."""
    import ray

    from ...common.providers.ray import get_controller_actor_handle

    ray_address = ray_address or config.get("NDIF_RAY_ADDRESS")
    ensure_ray_connected(ray_address)
    return ray.get(get_controller_actor_handle().status.remote())


__all__ = ["status", "NDIFConnectivityError"]
