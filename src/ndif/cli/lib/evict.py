"""Programmatic implementation of ``ndif evict``."""

from __future__ import annotations

import asyncio
from typing import Optional

from ...common.providers.ray import get_controller_actor_handle
from ._common import NDIFConnectivityError, OnMessage, check_prereqs, emit, ensure_ray_connected
from .session import get_env
from .util import (
    get_current_deployments,
    get_model_key,
    notify_dispatcher,
)


def evict(
    *,
    checkpoints: Optional[list[tuple[str, Optional[str]]]] = None,
    model_keys: Optional[list[str]] = None,
    evict_all: bool = False,
    flush_cache: bool = False,
    ray_address: Optional[str] = None,
    broker_url: Optional[str] = None,
    on_message: OnMessage = None,
) -> dict:
    """Evict models from the cluster.

    Provide exactly one of: ``checkpoints``, ``model_keys``, ``evict_all=True``,
    ``flush_cache=True``.

    Returns:
        - ``flush_cache``: ``{"flushed": [...], "memory_freed_bytes": int, "node_count": int}``
        - otherwise: ``{"results": [{"model_key", "status", "freed_gpus", "freed_memory_gbs"}]}``
    """
    import ray

    modes = [bool(checkpoints), bool(model_keys), evict_all, flush_cache]
    if sum(modes) != 1:
        raise ValueError(
            "Specify exactly one of: checkpoints, model_keys, evict_all, flush_cache"
        )

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")
    check_prereqs(broker_url, ray_address)

    emit(on_message, f"Connecting to Ray at {ray_address}...")
    ensure_ray_connected(ray_address)
    controller = get_controller_actor_handle()

    if flush_cache:
        emit(on_message, "Flushing WARM cache from all nodes...")
        node_results = ray.get(controller.flush_warm_cache.remote())

        flushed: list[str] = []
        total_memory = 0
        for node_id, result in node_results.items():
            flushed.extend(result.get("flushed", []))
            total_memory += result.get("memory_freed_bytes", 0)
            if result.get("flushed"):
                emit(
                    on_message,
                    f"  Node {node_id[:8]}...: {len(result['flushed'])} model(s), "
                    f"{result['memory_freed_bytes'] / (1024**3):.2f} GB freed",
                )
        return {
            "flushed": flushed,
            "memory_freed_bytes": total_memory,
            "node_count": len(node_results),
        }

    if evict_all:
        target_keys = [
            d["model_key"] for d in get_current_deployments(level="HOT")
            if "model_key" in d
        ]
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

    emit(on_message, f"Evicting {len(target_keys)} model(s)...")
    results = ray.get(controller.evict.remote(model_keys=target_keys))

    out: list[dict] = []
    for mk, r in results.items():
        if r["status"] == "not_found":
            emit(on_message, f"  ✗ {mk}: not found")
            out.append({
                "model_key": mk, "status": "not_found",
                "freed_gpus": 0, "freed_memory_gbs": 0.0,
            })
        else:
            emit(on_message, f"  ✓ {mk}: evicted")
            out.append({
                "model_key": mk,
                "status": "evicted",
                "freed_gpus": r.get("freed_gpus", 0),
                "freed_memory_gbs": r.get("freed_memory_gbs", 0.0),
            })
            try:
                asyncio.run(notify_dispatcher(broker_url, "evict", mk))
            except Exception:
                pass

    return {"results": out}


__all__ = ["evict", "NDIFConnectivityError"]
