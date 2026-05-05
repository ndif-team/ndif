"""Programmatic implementation of ``ndif deploy``.

Imported by the Click wrapper in ``cli/commands/deploy.py`` and by the
dashboard backend so admin actions exercise exactly the same code path.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from ...common.providers.ray import get_controller_actor_handle
from ...common.schema.deployment_config import DeploymentConfig
from ._common import (
    NDIFConnectivityError,
    OnMessage,
    check_prereqs,
    emit,
    ensure_ray_connected,
    normalize_specs,
)
from .session import get_env
from .util import (
    get_current_deployments,
    get_model_key,
    notify_dispatcher,
    wait_for_model_ready,
)


def deploy(
    specs: list[dict],
    *,
    sync: bool = False,
    ray_address: Optional[str] = None,
    broker_url: Optional[str] = None,
    on_message: OnMessage = None,
) -> dict:
    """Deploy a set of models on the NDIF controller.

    Args:
        specs: List of model spec dicts. Required key: ``checkpoint``.
            Optional keys: ``revision``, ``pinned``, ``actor_class``,
            ``padding_factor``, ``execution_timeout_seconds``.
        sync: If True, evict any current HOT models that are not in ``specs``
            before deploying. Mirrors ``ndif deploy --sync``.
        ray_address: Ray address (defaults to ``NDIF_RAY_ADDRESS``).
        broker_url: Broker URL (defaults to ``NDIF_BROKER_URL``).
        on_message: Optional progress callback. Receives one human-readable
            line per CLI ``echo`` so the Click wrapper can pass through
            ``click.echo`` and the dashboard can stream progress.

    Returns:
        ``{"deployments": [<entry>...], "evictions": [model_key, ...]}``
        where each ``<entry>`` is
        ``{"checkpoint", "model_key", "status", "error"}``.

    Raises:
        NDIFConnectivityError: If broker or Ray is unreachable.
        ValueError: If a spec is missing ``checkpoint``.
    """
    import ray

    specs = normalize_specs(specs)

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")
    check_prereqs(broker_url, ray_address)

    # Generate model keys
    model_keys_map: dict[str, dict] = {}
    for spec in specs:
        rev_str = f" (revision: {spec['revision']})" if spec["revision"] else ""
        emit(on_message, f"Generating model key for {spec['checkpoint']}{rev_str}...")
        model_key = get_model_key(spec["checkpoint"], spec["revision"])
        model_keys_map[model_key] = spec
        emit(on_message, f"  Model key: {model_key}")

    emit(on_message, f"Connecting to Ray at {ray_address}...")
    ensure_ray_connected(ray_address)

    controller = get_controller_actor_handle()

    deployments_result: list[dict] = []
    all_evictions: list[str] = []

    if sync:
        all_evictions.extend(
            _sync_evict_extra_models(controller, model_keys_map, broker_url, on_message)
        )

    # Order matches the existing CLI: non-pinned batch, then pinned batch.
    for is_pinned in (False, True):
        batch_keys = [
            mk for mk, sp in model_keys_map.items()
            if sp["pinned"] == is_pinned
        ]
        if not batch_keys:
            continue

        label = " (pinned)" if is_pinned else ""
        emit(on_message, f"\nDeploying {len(batch_keys)} model(s){label}...")

        configs = {
            mk: DeploymentConfig(
                pinned=is_pinned,
                actor_class=model_keys_map[mk]["actor_class"],
                padding_factor=model_keys_map[mk]["padding_factor"],
                execution_timeout_seconds=model_keys_map[mk]["execution_timeout_seconds"],
            )
            for mk in batch_keys
        }
        results = ray.get(controller._deploy.remote(deployments=configs))
        all_evictions.extend(results.get("evictions", []))

        result_status_map = results.get("result", {})
        models_to_init: list[str] = []

        for model_key in batch_keys:
            spec = model_keys_map[model_key]
            status = result_status_map.get(model_key, "UNKNOWN")
            entry = {
                "checkpoint": spec["checkpoint"],
                "model_key": model_key,
                "status": status,
                "error": None,
            }

            if status == "CANT_ACCOMMODATE":
                entry["error"] = "cannot be deployed on any node"
                emit(on_message, f"  ✗ {model_key}: {entry['error']}")
            elif status == "DEPLOYED":
                emit(on_message, f"  ✓ {model_key}: already deployed")
            elif status in ("FREE", "CACHED_AND_FREE"):
                emit(on_message, f"  ⋯ {model_key}: provisioned, initializing...")
                models_to_init.append(model_key)
            elif status in ("FULL", "CACHED_AND_FULL"):
                entry["error"] = "no capacity available"
                emit(on_message, f"  ✗ {model_key}: {entry['error']}")
            else:
                entry["error"] = f"unknown status ({status})"
                emit(on_message, f"  ? {model_key}: {entry['error']}")

            deployments_result.append(entry)

        for model_key in models_to_init:
            entry = next(d for d in deployments_result if d["model_key"] == model_key)
            try:
                if wait_for_model_ready(model_key):
                    entry["status"] = "READY"
                    emit(on_message, f"  ✓ {model_key}: ready")
                    asyncio.run(notify_dispatcher(broker_url, "deploy", model_key))
                else:
                    entry["status"] = "TIMEOUT"
                    entry["error"] = "initialization timed out"
                    emit(on_message, f"  ✗ {model_key}: initialization timed out")
            except Exception as e:
                entry["status"] = "ERROR"
                entry["error"] = str(e)
                emit(on_message, f"  ✗ {model_key}: initialization failed - {e}")

    if all_evictions:
        emit(on_message, "\nEvictions:")
        for eviction in all_evictions:
            emit(on_message, f"  - {eviction}")
            try:
                asyncio.run(notify_dispatcher(broker_url, "evict", eviction))
            except Exception:
                pass

    return {"deployments": deployments_result, "evictions": all_evictions}


def _sync_evict_extra_models(
    controller,
    desired_keys: dict,
    broker_url: Optional[str],
    on_message: OnMessage,
) -> list[str]:
    """Evict HOT models that are not in the desired set."""
    import ray

    current_deployments = get_current_deployments(level="HOT")
    current_keys = {dep["model_key"] for dep in current_deployments}
    to_evict = current_keys - set(desired_keys.keys())
    if not to_evict:
        return []

    emit(on_message, f"\nSync mode: evicting {len(to_evict)} model(s) not in desired set...")

    results = ray.get(controller.evict.remote(model_keys=list(to_evict)))
    evicted: list[str] = []

    for model_key, result in results.items():
        if result["status"] == "not_found":
            emit(on_message, f"  - {model_key}: not found")
        else:
            emit(on_message, f"  ✓ {model_key}: evicted")
            evicted.append(model_key)
            try:
                asyncio.run(notify_dispatcher(broker_url, "evict", model_key))
            except Exception:
                pass

    return evicted


# Re-export the connectivity error so callers can `from ...lib.deploy import
# NDIFConnectivityError` if they only want a single import line.
__all__ = ["deploy", "NDIFConnectivityError"]
