"""Programmatic implementation of ``ndif restart`` (Ray controller only)."""

from __future__ import annotations

from typing import Optional

from .. import config
from ._common import NDIFConnectivityError, OnMessage, emit, ensure_ray_connected
from .models import get_model_key, wait_for_replica_ready


def restart(
    checkpoint: Optional[str] = None,
    *,
    revision: Optional[str] = None,
    model_key: Optional[str] = None,
    replica: Optional[str] = None,
    ray_address: Optional[str] = None,
    on_message: OnMessage = None,
) -> dict:
    """Restart replicas of a model by killing and awaiting respawn.

    Either ``checkpoint`` (+ optional ``revision``) or a ready ``model_key``
    must be given. Restarts every HOT replica by default; ``replica`` targets
    one. Blocks on ``wait_for_replica_ready`` until the new actor is ready.

    Returns ``{"model_key", "replicas": [{"replica_id", "status", ...}]}``.
    """
    import ray

    from ...common.providers.ray import get_controller_actor_handle

    ray_address = ray_address or config.get("NDIF_RAY_ADDRESS")

    if model_key is None:
        if not checkpoint:
            raise ValueError("Either model_key or checkpoint is required")
        rev_str = f" (revision: {revision})" if revision else ""
        emit(on_message, f"Generating model key for {checkpoint}{rev_str}...")
        model_key = get_model_key(checkpoint, revision)
        emit(on_message, f"  Model key: {model_key}")

    emit(on_message, f"Connecting to Ray at {ray_address}...")
    ensure_ray_connected(ray_address)

    controller = get_controller_actor_handle()

    response = ray.get(controller.get_deployment.remote(model_key))
    replica_ids = [r.replica_id for r in response.replicas]
    if not replica_ids:
        emit(on_message, f"  ✗ {model_key}: no HOT replicas to restart")
        return {"model_key": model_key, "replicas": []}

    if replica is not None:
        if replica not in replica_ids:
            emit(on_message,
                 f"  ✗ {model_key}: replica {replica} not found "
                 f"(have: {', '.join(replica_ids) or '(none)'})")
            return {"model_key": model_key, "replicas": []}
        replica_ids = [replica]

    emit(on_message, f"Restarting {len(replica_ids)} replica(s) of {model_key}...")

    out = [_restart_one(model_key, rid, on_message) for rid in replica_ids]
    return {"model_key": model_key, "replicas": out}


def _restart_one(model_key: str, replica_id: str, on_message: OnMessage) -> dict:
    """Kill + respawn one replica, then wait for it to come back up."""
    import ray

    from ...common.providers.ray import get_model_actor_handle

    try:
        actor = get_model_actor_handle(model_key, replica_id)
    except Exception as e:
        emit(on_message, f"  ✗ [{replica_id}] could not get actor handle: {e}")
        return {"replica_id": replica_id, "status": "error", "error": str(e)}

    emit(on_message, f"  ⋯ [{replica_id}] killing and awaiting respawn...")
    try:
        # no_restart=False: max_restarts=-1 on the actor means Ray respawns it.
        ray.kill(actor, no_restart=False)
    except Exception as e:
        emit(on_message, f"  ✗ [{replica_id}] kill failed: {e}")
        return {"replica_id": replica_id, "status": "error", "error": str(e)}

    try:
        # Blocks until the respawned actor serves, or raises why it cannot.
        wait_for_replica_ready(model_key, replica_id)
        emit(on_message, f"  ✓ [{replica_id}] restarted")
        return {"replica_id": replica_id, "status": "restarted"}
    except Exception as e:
        emit(on_message, f"  ✗ [{replica_id}] failed: {e}")
        return {"replica_id": replica_id, "status": "error", "error": str(e)}


__all__ = ["restart", "NDIFConnectivityError"]
