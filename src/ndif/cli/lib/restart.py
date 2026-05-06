"""Programmatic implementation of ``ndif restart``."""

from __future__ import annotations

from typing import Optional

from ...common.providers.ray import get_model_actor_handle
from ._common import NDIFConnectivityError, OnMessage, emit, ensure_ray_connected
from .session import get_env
from .util import get_model_key, wait_for_model_ready


def restart(
    checkpoint: Optional[str] = None,
    *,
    revision: Optional[str] = None,
    model_key: Optional[str] = None,
    ray_address: Optional[str] = None,
    timeout: int = 300,
    on_message: OnMessage = None,
) -> dict:
    """Restart a single model actor.

    Either ``checkpoint`` (+ optional ``revision``) or a fully-formed
    ``model_key`` must be supplied. ``model_key`` short-circuits the nnsight
    LanguageModel-loading step and is much faster from the dashboard's
    perspective when we already know the key from ``/status``.

    After ``ray.kill(no_restart=False)`` initiates the respawn we block on
    ``wait_for_model_ready`` until the new actor instance is ready (or
    ``timeout`` elapses). Without that wait, callers — the dashboard's
    "Restart" button in particular — would see "✓ restarted" the instant
    the kill went through, while the model is actually still cold-loading.

    Returns ``{"model_key", "status"}`` where ``status`` is ``"restarted"``
    on success, ``"timeout"`` if readiness wasn't observed within
    ``timeout`` seconds, or ``"error"`` on initialization failure.
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
    actor = get_model_actor_handle(model_key)

    emit(on_message, f"Killing deployment for {model_key}...")
    ray.kill(actor, no_restart=False)

    emit(on_message, "Waiting for actor to come back up...")
    try:
        if wait_for_model_ready(model_key, timeout=timeout):
            emit(on_message, "✓ Restart successful!")
            return {"model_key": model_key, "status": "restarted"}
        emit(on_message, f"✗ Restart timed out after {timeout}s")
        return {
            "model_key": model_key,
            "status": "timeout",
            "error": f"actor did not become ready within {timeout}s",
        }
    except Exception as e:
        emit(on_message, f"✗ Restart failed: {e}")
        return {"model_key": model_key, "status": "error", "error": str(e)}


__all__ = ["restart", "NDIFConnectivityError"]
