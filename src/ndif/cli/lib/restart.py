"""Programmatic implementation of ``ndif restart``."""

from __future__ import annotations

from typing import Optional

from ...common.providers.ray import (
    get_controller_actor_handle,
    get_model_actor_handle,
)
from ._common import NDIFConnectivityError, OnMessage, emit, ensure_ray_connected
from .session import get_env
from .util import get_model_key, wait_for_replica_ready


def restart(
    checkpoint: Optional[str] = None,
    *,
    revision: Optional[str] = None,
    model_key: Optional[str] = None,
    replica: Optional[str] = None,
    ray_address: Optional[str] = None,
    timeout: int = 300,
    on_message: OnMessage = None,
) -> dict:
    """Restart replicas of a model.

    Either ``checkpoint`` (+ optional ``revision``) or a fully-formed
    ``model_key`` must be supplied. ``model_key`` short-circuits the nnsight
    LanguageModel-loading step and is much faster from the dashboard's
    perspective when we already know the key from ``/status``.

    If ``replica`` is provided, only that specific replica is restarted;
    otherwise every HOT replica of the model is restarted in turn.

    After ``ray.kill(no_restart=False)`` initiates the respawn we block on
    ``wait_for_replica_ready`` until the new actor instance is ready (or
    ``timeout`` elapses). Without that wait, callers — the dashboard's
    "Restart" button in particular — would see "✓ restarted" the instant
    the kill went through, while the model is actually still cold-loading.

    Returns ``{"model_key", "replicas": [{"replica_id", "status", ...}]}``
    where each per-replica ``status`` is ``"restarted"`` on success,
    ``"timeout"`` if readiness wasn't observed within ``timeout`` seconds,
    or ``"error"`` on initialization failure.
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

    controller = get_controller_actor_handle()

    # Resolve which replicas to restart from current cluster state.
    response = ray.get(controller.get_deployment.remote(model_key))
    replica_ids = [r.replica_id for r in response.replicas]
    if not replica_ids:
        emit(on_message, f"  ✗ {model_key}: no HOT replicas to restart")
        return {"model_key": model_key, "replicas": []}

    if replica is not None:
        if replica not in replica_ids:
            emit(
                on_message,
                f"  ✗ {model_key}: replica {replica} not found "
                f"(have: {', '.join(replica_ids) or '(none)'})",
            )
            return {"model_key": model_key, "replicas": []}
        replica_ids = [replica]

    emit(
        on_message,
        f"Restarting {len(replica_ids)} replica(s) of {model_key}...",
    )

    out: list[dict] = []
    for rid in replica_ids:
        out.append(_restart_one(model_key, rid, timeout, on_message))

    return {"model_key": model_key, "replicas": out}


def _restart_one(
    model_key: str,
    replica_id: str,
    timeout: int,
    on_message: OnMessage,
) -> dict:
    """Kill + respawn one replica, then wait for it to come back up."""
    import ray

    try:
        actor = get_model_actor_handle(model_key, replica_id)
    except Exception as e:
        emit(on_message, f"  ✗ [{replica_id}] could not get actor handle: {e}")
        return {"replica_id": replica_id, "status": "error", "error": str(e)}

    emit(on_message, f"  ⋯ [{replica_id}] killing and awaiting respawn...")
    try:
        ray.kill(actor, no_restart=False)
    except Exception as e:
        emit(on_message, f"  ✗ [{replica_id}] kill failed: {e}")
        return {"replica_id": replica_id, "status": "error", "error": str(e)}

    try:
        if wait_for_replica_ready(model_key, replica_id, timeout=timeout):
            emit(on_message, f"  ✓ [{replica_id}] restarted")
            return {"replica_id": replica_id, "status": "restarted"}
        emit(on_message, f"  ✗ [{replica_id}] timed out after {timeout}s")
        return {
            "replica_id": replica_id,
            "status": "timeout",
            "error": f"actor did not become ready within {timeout}s",
        }
    except Exception as e:
        emit(on_message, f"  ✗ [{replica_id}] failed: {e}")
        return {"replica_id": replica_id, "status": "error", "error": str(e)}


__all__ = ["restart", "NDIFConnectivityError"]
