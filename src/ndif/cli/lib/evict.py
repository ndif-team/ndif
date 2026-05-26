"""Programmatic implementation of ``ndif evict``."""

from __future__ import annotations

from typing import Optional

from ...common.providers.ray import get_controller_actor_handle
from ._common import NDIFConnectivityError, OnMessage, check_prereqs, emit, ensure_ray_connected
from .session import get_env
from .util import (
    get_current_deployments,
    get_model_key,
    notify_reconcile,
)


def evict(
    *,
    checkpoints: Optional[list[tuple[str, Optional[str]]]] = None,
    model_keys: Optional[list[str]] = None,
    evict_all: bool = False,
    replica: Optional[str] = None,
    ray_address: Optional[str] = None,
    broker_url: Optional[str] = None,
    on_message: OnMessage = None,
) -> dict:
    """Evict models from the cluster.

    Provide exactly one of: ``checkpoints``, ``model_keys``, ``evict_all=True``.

    By default every HOT and WARM replica of each target model is evicted.
    Pass ``replica=<replica_id>`` together with a single-model selector
    (one checkpoint or one model_key) to target a single replica instead.

    Returns:
        ``{"results": [{"model_key", "status", "replicas": [...records]}]}``
        where each per-replica record carries
        ``{"replica_id", "freed_gpus", "freed_memory_gbs"}``.
    """
    import ray

    modes = [bool(checkpoints), bool(model_keys), evict_all]
    if sum(modes) != 1:
        raise ValueError(
            "Specify exactly one of: checkpoints, model_keys, evict_all"
        )

    if replica is not None:
        # Single-replica targeting needs a single model_key, no fan-out.
        if evict_all:
            raise ValueError("--replica cannot be combined with evict_all")
        if (checkpoints and len(checkpoints) > 1) or (
            model_keys and len(model_keys) > 1
        ):
            raise ValueError(
                "--replica targets a single replica — supply exactly one model"
            )

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")
    check_prereqs(broker_url, ray_address)

    emit(on_message, f"Connecting to Ray at {ray_address}...")
    ensure_ray_connected(ray_address)
    controller = get_controller_actor_handle()

    if evict_all:
        target_keys = sorted({
            d["model_key"] for d in get_current_deployments(level="HOT")
            if "model_key" in d
        })
        if not target_keys:
            return {"results": []}
    elif checkpoints:
        target_keys = []
        for cp, rev in checkpoints:
            mk = get_model_key(cp, rev)
            target_keys.append(mk)
            emit(on_message, f"  Model key for {cp}: {mk}")
    else:
        target_keys = list(model_keys or [])

    emit(on_message, f"Evicting from {len(target_keys)} model(s)...")

    out: list[dict] = []
    for mk in target_keys:
        response = ray.get(controller.evict.remote(mk, replica))
        replicas = response.replicas
        if not replicas:
            emit(on_message, f"  ✗ {mk}: nothing to evict")
            out.append({"model_key": mk, "status": "not_found", "replicas": []})
            continue

        emit(on_message, f"  ✓ {mk}: evicted {len(replicas)} replica(s)")
        records = []
        for r in replicas:
            freed_gpu_bytes = sum(r.gpus.values())
            freed_gbs = r.size_bytes / 1024 / 1024 / 1024
            emit(
                on_message,
                f"      - [{r.replica_id}] {len(r.gpus)} GPU(s), "
                f"{round(freed_gbs, 4)} GB",
            )
            records.append(
                {
                    "replica_id": r.replica_id,
                    "freed_gpus": len(r.gpus),
                    "freed_gpu_memory_bytes": freed_gpu_bytes,
                    "freed_memory_gbs": freed_gbs,
                }
            )
        out.append({"model_key": mk, "status": "evicted", "replicas": records})

    # Tell the dispatcher to refresh its pool for every model we touched
    # (even the not-found ones — defensive against the dispatcher having a
    # stale entry for a model the controller no longer knows about).
    notify_reconcile(broker_url, target_keys)

    return {"results": out}


__all__ = ["evict", "NDIFConnectivityError"]
