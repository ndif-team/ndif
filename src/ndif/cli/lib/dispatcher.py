"""Calls to the Dispatcher actor: queue state, kill, reconcile.

The dispatcher holds what the Controller does not: each model's queue and the
requests its replicas are running. ``ndif queue`` and ``ndif kill`` read and act
on that, and a deploy or evict tells it to re-sync the models it touched.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .. import config
from ._common import NDIFConnectivityError, ensure_ray_connected


def queue_state(*, ray_address: Optional[str] = None) -> dict:
    """Return ``{"processors": {model_key: state}}``, one entry per model the
    dispatcher has seen a request for."""
    import ray

    from ...common.providers.ray import get_dispatcher_actor_handle

    ray_address = ray_address or config.get("NDIF_RAY_ADDRESS")
    ensure_ray_connected(ray_address)
    return ray.get(get_dispatcher_actor_handle().queue_state.remote())


def kill(request_id: str, *, ray_address: Optional[str] = None) -> dict:
    """Cancel a request, queued or executing.

    Returns ``{"status", "message"}``, where ``status`` is
    ``removed_from_queue``, ``cancelled_execution`` or ``not_found``.
    """
    import ray

    from ...common.providers.ray import get_dispatcher_actor_handle

    ray_address = ray_address or config.get("NDIF_RAY_ADDRESS")
    ensure_ray_connected(ray_address)
    return ray.get(get_dispatcher_actor_handle().kill.remote(request_id))


def reconcile(model_keys: Iterable[str]) -> None:
    """Tell the dispatcher to re-sync these models' replica pools.

    For the end of a deploy or evict, on the Ray connection that made it. The
    change has already succeeded on the Controller, so this does not wait for
    the dispatcher and a failure to reach it is swallowed.
    """
    from ...common.providers.ray import get_dispatcher_actor_handle

    try:
        dispatcher = get_dispatcher_actor_handle()
        for model_key in sorted({mk for mk in model_keys if mk}):
            dispatcher.reconcile.remote(model_key)
    except Exception:
        pass


__all__ = ["queue_state", "kill", "reconcile", "NDIFConnectivityError"]
