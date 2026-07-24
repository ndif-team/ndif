"""Programmatic implementation of ``ndif deploy``.

Deploy is **additive**: every call asks the controller to place ``replicas``
new replicas of each model, regardless of what is already running. Use
``sync=True`` (with a config file) to instead reconcile the cluster to match
the config exactly.

Talks to the Ray controller only. The dispatcher discovers newly-placed
replicas lazily — a Processor created on the next request for a model provisions
against ``controller.get_deployment`` — so no out-of-band notification is
needed here.
"""

from __future__ import annotations

from typing import Optional

from .. import config
from ._common import (
    NDIFConnectivityError,
    OnMessage,
    emit,
    ensure_ray_connected,
    normalize_specs,
)
from .events import notify_reconcile
from .models import get_current_deployments, get_model_key, wait_for_replica_ready


def deploy(
    specs: list[dict],
    *,
    sync: bool = False,
    ray_address: Optional[str] = None,
    redis_url: Optional[str] = None,
    on_message: OnMessage = None,
) -> dict:
    """Deploy a set of models on the NDIF controller.

    Args:
        specs: model spec dicts. Required: ``checkpoint``. Optional: ``revision``,
            ``pinned``, ``replicas`` (default 1), ``trusted`` (default False),
            ``dtype``, ``actor_class``, ``envoy_class``, ``padding_factor``,
            ``execution_timeout_seconds``, ``model_key``.
        sync: reconcile the cluster to match ``specs`` exactly (evict extras,
            trim/grow per model). Without it, each call is purely additive.
        ray_address: Ray address (defaults to ``NDIF_RAY_ADDRESS``).
        on_message: progress callback, one human-readable line per step.

    Returns:
        ``{"deployments": [...], "evictions": [...]}``.

    Raises:
        NDIFConnectivityError: if Ray is unreachable.
        ValueError: if a spec is missing ``checkpoint``.
    """
    import ray

    # Deferred so the bare CLI (help, service lifecycle) doesn't import the
    # provider/nnsight stack at startup.
    from ...common.providers.ray import get_controller_actor_handle
    from ...common.schema.controller import DeploymentConfig

    raw_specs = list(specs)
    specs = normalize_specs(raw_specs)
    # normalize_specs canonicalizes the deploy-path fields but not dtype; carry
    # it across so a config file / --dtype picks the load + size-estimate dtype.
    for raw, spec in zip(raw_specs, specs):
        spec["dtype"] = raw.get("dtype")
    ray_address = ray_address or config.get("NDIF_RAY_ADDRESS")

    # Generate model keys up front so we can dedupe and pass the canonical form
    # into both sync reconciliation and the deploy call.
    model_keys_map: dict[str, dict] = {}
    for spec in specs:
        rev_str = f" (revision: {spec['revision']})" if spec["revision"] else ""
        if spec.get("model_key"):
            model_key = spec["model_key"]
            emit(on_message, f"Using provided model key for {spec['checkpoint']}{rev_str}: {model_key}")
        else:
            emit(on_message, f"Generating model key for {spec['checkpoint']}{rev_str}...")
            model_key = get_model_key(
                spec["checkpoint"], spec["revision"], spec.get("envoy_class")
            )
            emit(on_message, f"  Model key: {model_key}")
        model_keys_map[model_key] = spec

    emit(on_message, f"Connecting to Ray at {ray_address}...")
    ensure_ray_connected(ray_address)

    controller = get_controller_actor_handle()

    deployments_result: list[dict] = []
    all_evictions: list[dict] = []

    if sync:
        evicted, additive_specs = _sync_reconcile(controller, model_keys_map, on_message)
        all_evictions.extend(evicted)
        # Only place the remaining shortfall — anything already satisfied was
        # filtered out by _sync_reconcile.
        model_keys_map = additive_specs

    # Group by pinned-ness so we call _deploy in two batches (non-pinned first,
    # then pinned). The configs carry the replica count so the controller knows
    # how many NEW replicas to add per model on this call.
    for is_pinned in (False, True):
        batch_keys = [mk for mk, sp in model_keys_map.items() if sp["pinned"] == is_pinned]
        if not batch_keys:
            continue

        label = " (pinned)" if is_pinned else ""
        emit(on_message, f"\nDeploying {len(batch_keys)} model(s){label}...")

        configs = {
            mk: DeploymentConfig(
                pinned=is_pinned,
                replicas=int(model_keys_map[mk].get("replicas") or 1),
                trusted=bool(model_keys_map[mk].get("trusted", False)),
                dtype=model_keys_map[mk].get("dtype"),
                actor_class=model_keys_map[mk]["actor_class"],
                padding_factor=model_keys_map[mk]["padding_factor"],
                execution_timeout_seconds=model_keys_map[mk]["execution_timeout_seconds"],
            )
            for mk in batch_keys
        }
        response = ray.get(controller._deploy.remote(deployments=configs))

        # Surface evictions the controller took to make room.
        for mk, rid in response.evictions:
            all_evictions.append({"model_key": mk, "replica_id": rid})

        for model_key in batch_keys:
            spec = model_keys_map[model_key]
            result = response.results.get(model_key)
            replicas = list(result.replicas) if result else []
            entry: dict = {
                "checkpoint": spec["checkpoint"],
                "model_key": model_key,
                "replicas": replicas,
                "status": "PENDING",
                "error": result.error if result else None,
            }

            if entry["error"]:
                entry["status"] = "ERROR"
                emit(on_message, f"  ✗ {model_key}: {entry['error']}")
            elif not replicas:
                entry["status"] = "ERROR"
                entry["error"] = "no replicas placed"
                emit(on_message, f"  ✗ {model_key}: no replicas placed")
            else:
                emit(on_message, f"  ⋯ {model_key}: provisioned {len(replicas)} replica(s), initializing...")
                for rid in replicas:
                    emit(on_message, f"      - [{rid}] waiting for ready")

            deployments_result.append(entry)

        # Block until each newly-placed replica is __ray_ready__, per-replica so
        # the user sees progress for each one.
        for entry in deployments_result:
            if entry["model_key"] not in batch_keys:
                continue
            if entry["error"] is not None or not entry["replicas"]:
                continue

            ready, failed = [], []
            for rid in entry["replicas"]:
                try:
                    if wait_for_replica_ready(entry["model_key"], rid):
                        emit(on_message, f"  ✓ {entry['model_key']} [{rid}]: ready")
                        ready.append(rid)
                    else:
                        emit(on_message, f"  ✗ {entry['model_key']} [{rid}]: initialization timed out")
                        failed.append((rid, "initialization timed out"))
                except Exception as e:
                    emit(on_message, f"  ✗ {entry['model_key']} [{rid}]: initialization failed - {e}")
                    failed.append((rid, str(e)))

            if failed and not ready:
                entry["status"] = "ERROR"
                entry["error"] = "; ".join(f"[{rid}] {err}" for rid, err in failed)
            elif failed:
                entry["status"] = "PARTIAL"
                entry["error"] = "; ".join(f"[{rid}] {err}" for rid, err in failed)
            else:
                entry["status"] = "READY"

    if all_evictions:
        emit(on_message, "\nEvictions:")
        for eviction in all_evictions:
            emit(on_message, f"  - {eviction['model_key']} [{eviction['replica_id']}]")

    # Nudge any already-active dispatcher Processors to refresh their replica
    # pool for models we deployed to or evicted from.
    touched = {e["model_key"] for e in deployments_result if e.get("model_key")}
    touched |= {e["model_key"] for e in all_evictions if e.get("model_key")}
    notify_reconcile(redis_url, touched)

    return {"deployments": deployments_result, "evictions": all_evictions}


def _sync_reconcile(
    controller,
    desired_map: dict[str, dict],
    on_message: OnMessage,
) -> tuple[list[dict], dict[str, dict]]:
    """Make the cluster match ``desired_map`` exactly.

    Evicts every HOT model_key not desired, then per model trims excess replicas
    or reduces the spec's count to the shortfall so the caller only adds what's
    missing. Returns ``(evictions, remaining_specs)``.
    """
    import ray

    evictions: list[dict] = []
    remaining: dict[str, dict] = {}

    current_hot = get_current_deployments(level="HOT")
    current_by_mk: dict[str, list[str]] = {}
    for dep in current_hot:
        mk = dep.get("model_key")
        rid = dep.get("replica_id")
        if mk and rid:
            current_by_mk.setdefault(mk, []).append(rid)

    desired_keys = set(desired_map.keys())
    extras = [mk for mk in current_by_mk if mk not in desired_keys]

    if extras:
        emit(on_message, f"\nSync mode: evicting {len(extras)} model(s) not in desired set...")
        for mk in extras:
            response = ray.get(controller.evict.remote(mk, None))
            for r in response.replicas:
                emit(on_message, f"  ✓ {mk} [{r.replica_id}]: evicted")
                evictions.append({"model_key": mk, "replica_id": r.replica_id})

    for mk, spec in desired_map.items():
        wanted = int(spec.get("replicas") or 1)
        have = len(current_by_mk.get(mk, []))

        if have >= wanted:
            surplus = current_by_mk.get(mk, [])[wanted:]
            if surplus:
                emit(on_message, f"\nSync mode: trimming {len(surplus)} extra replica(s) of {mk}...")
            for rid in surplus:
                response = ray.get(controller.evict.remote(mk, rid))
                for r in response.replicas:
                    emit(on_message, f"  ✓ {mk} [{r.replica_id}]: evicted")
                    evictions.append({"model_key": mk, "replica_id": r.replica_id})
            continue

        adjusted = dict(spec)
        adjusted["replicas"] = wanted - have
        remaining[mk] = adjusted

    return evictions, remaining


__all__ = ["deploy", "NDIFConnectivityError"]
