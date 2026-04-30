"""Programmatic implementation of ``ndif restart``."""

from __future__ import annotations

from typing import Optional

from ._common import NDIFConnectivityError, OnMessage, emit, ensure_ray_connected
from .session import get_env
from .util import get_actor_handle, get_model_key


def restart(
    checkpoint: Optional[str] = None,
    *,
    revision: Optional[str] = None,
    model_key: Optional[str] = None,
    ray_address: Optional[str] = None,
    on_message: OnMessage = None,
) -> dict:
    """Restart a single model actor.

    Either ``checkpoint`` (+ optional ``revision``) or a fully-formed
    ``model_key`` must be supplied. ``model_key`` short-circuits the nnsight
    LanguageModel-loading step and is much faster from the dashboard's
    perspective when we already know the key from ``/status``.
    """
    import ray

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")

    if model_key is None:
        if not checkpoint:
            raise ValueError("Either model_key or checkpoint is required")
        rev_str = f" (revision: {revision})" if revision else ""
        emit(on_message, f"Generating model key for {checkpoint}{rev_str}...")
        model_key = get_model_key(checkpoint, revision)
        emit(on_message, f"  Model key: {model_key}")

    emit(on_message, f"Connecting to Ray at {ray_address}...")
    ensure_ray_connected(ray_address)

    emit(on_message, f"Getting actor handle for {model_key}...")
    actor = get_actor_handle(model_key)

    emit(on_message, f"Restarting deployment for {model_key}...")
    ray.kill(actor, no_restart=False)
    emit(on_message, "✓ Restart successful!")
    return {"model_key": model_key, "status": "restarted"}


__all__ = ["restart", "NDIFConnectivityError"]
