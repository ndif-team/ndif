"""Programmatic implementation of ``ndif deploy``.

Imported by the Click wrapper in ``cli/commands/deploy.py`` and by the
dashboard backend so admin actions exercise exactly the same code path.

Deploy is **additive**: every call asks the controller to place ``replicas``
new replicas of each model, regardless of what is already running. Use
``sync=True`` (with a config file) to instead reconcile the cluster to match
the config exactly — that path adds, removes, and trims replicas as needed.
"""

from __future__ import annotations

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
    notify_reconcile,
    wait_for_replica_ready,
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
            Optional keys: ``revision``, ``pinned``, ``replicas`` (default 1),
            ``actor_class``, ``envoy_class``, ``padding_factor``,
            ``execution_timeout_seconds``.
            ``envoy_class`` is the dotted import path of the nnsight wrapper
            (default: ``nnsight.modeling.language.LanguageModel``); it
            selects which class is used to compute the model_key and which
            class the server reconstructs.
        sync: If True, reconcile the cluster to match ``specs`` exactly —
            models not in ``specs`` have all their replicas evicted, and
            models in ``specs`` are deployed/trimmed to match the requested
            replica count. Without ``sync``, each call is purely additive.
        ray_address: Ray address (defaults to ``NDIF_RAY_ADDRESS``).
        broker_url: Broker URL (defaults to ``NDIF_BROKER_URL``).
        on_message: Optional progress callback. Receives one human-readable
            line per CLI ``echo`` so the Click wrapper can pass through
            ``click.echo`` and the dashboard can stream progress.

    Returns:
        ``{"deployments": [<entry>...], "evictions": [...]}``
        where each ``<entry>`` is
        ``{"checkpoint", "model_key", "replicas": [...], "status", "error"}``
        and each entry in ``evictions`` is
        ``{"model_key", "replica_id"}``.

    Raises:
        NDIFConnectivityError: If broker or Ray is unreachable.
        ValueError: If a spec is missing ``checkpoint``.
    """
    import ray

    specs = normalize_specs(specs)

    ray_address = ray_address or get_env("NDIF_RAY_ADDRESS")
    broker_url = broker_url or get_env("NDIF_BROKER_URL")
    check_prereqs(broker_url, ray_address)

    # Generate model keys for every spec up front so we can dedupe and pass
    # the canonical form into both sync reconciliation and the deploy call.
    model_keys_map: dict[str, dict] = {}
    for spec in specs:
        rev_str = f" (revision: {spec['revision']})" if spec["revision"] else ""
        # Callers that already hold the canonical model_key (e.g. the
        # dashboard's WARM redeploy path, which reads it off the existing
        # deployment card) can short-circuit get_model_key entirely.
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

    # Sync mode: bring the cluster to *exactly* the config. Evict models not
    # in the desired set, and per-model trim/grow to the requested count.
    if sync:
        evicted, additive_specs = _sync_reconcile(
            controller, model_keys_map, on_message
        )
        all_evictions.extend(evicted)
        # Only place the remaining shortfall — anything already satisfied
        # was filtered out by _sync_reconcile.
        model_keys_map = additive_specs

    # Group by pinned-ness so we can call _deploy in two batches (non-pinned
    # first, then pinned) — matches the historic CLI ordering. The configs
    # themselves carry the replica count so the controller knows how many
    # NEW replicas to add per model on this call.
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
                replicas=int(model_keys_map[mk].get("replicas") or 1),
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
                emit(
                    on_message,
                    f"  ⋯ {model_key}: provisioned {len(replicas)} replica(s), initializing...",
                )
                for rid in replicas:
                    emit(on_message, f"      - [{rid}] waiting for ready")

            deployments_result.append(entry)

        # Block until each newly-placed replica is __ray_ready__. Per-replica
        # so the user sees progress for each one rather than one combined
        # "ready" line.
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
                        emit(
                            on_message,
                            f"  ✗ {entry['model_key']} [{rid}]: initialization timed out",
                        )
                        failed.append((rid, "initialization timed out"))
                except Exception as e:
                    emit(
                        on_message,
                        f"  ✗ {entry['model_key']} [{rid}]: initialization failed - {e}",
                    )
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
            emit(
                on_message,
                f"  - {eviction['model_key']} [{eviction['replica_id']}]",
            )

    # Tell the dispatcher to refresh its replica pool for both the models
    # we deployed (new replicas to pick up) and any models whose replicas
    # the controller evicted to make room (their pools are now stale).
    touched: set[str] = set()
    for entry in deployments_result:
        if entry.get("model_key"):
            touched.add(entry["model_key"])
    for eviction in all_evictions:
        if eviction.get("model_key"):
            touched.add(eviction["model_key"])
    notify_reconcile(broker_url, touched)

    return {"deployments": deployments_result, "evictions": all_evictions}


def _sync_reconcile(
    controller,
    desired_map: dict[str, dict],
    on_message: OnMessage,
) -> tuple[list[dict], dict[str, dict]]:
    """Make the cluster match ``desired_map`` exactly.

    Steps:
        1. Evict every HOT model_key not in ``desired_map``.
        2. For each model in ``desired_map``, trim excess replicas if the
           cluster has more than the spec asks for. Reduce the spec's
           ``replicas`` count by however many already match so the caller
           only adds the shortfall.

    Returns ``(evictions, remaining_specs)`` where ``evictions`` is the list
    of ``{"model_key", "replica_id"}`` records evicted and
    ``remaining_specs`` is ``desired_map`` filtered to entries that still
    need replicas placed (with ``spec["replicas"]`` adjusted down to the
    shortfall).
    """
    import ray

    evictions: list[dict] = []
    remaining: dict[str, dict] = {}

    current_hot = get_current_deployments(level="HOT")
    # Build {model_key -> [replica_id, ...]} so we know the current count.
    current_by_mk: dict[str, list[str]] = {}
    for dep in current_hot:
        mk = dep.get("model_key")
        rid = dep.get("replica_id")
        if mk and rid:
            current_by_mk.setdefault(mk, []).append(rid)

    desired_keys = set(desired_map.keys())
    extras = [mk for mk in current_by_mk if mk not in desired_keys]

    if extras:
        emit(
            on_message,
            f"\nSync mode: evicting {len(extras)} model(s) not in desired set...",
        )
        for mk in extras:
            response = ray.get(controller.evict.remote(mk, None))
            for r in response.replicas:
                emit(on_message, f"  ✓ {mk} [{r.replica_id}]: evicted")
                evictions.append({"model_key": mk, "replica_id": r.replica_id})

    for mk, spec in desired_map.items():
        wanted = int(spec.get("replicas") or 1)
        have = len(current_by_mk.get(mk, []))

        if have >= wanted:
            # Trim excess (evict the surplus). Excess > 0 only when have>wanted.
            surplus = current_by_mk.get(mk, [])[wanted:]
            if surplus:
                emit(
                    on_message,
                    f"\nSync mode: trimming {len(surplus)} extra "
                    f"replica(s) of {mk}...",
                )
            for rid in surplus:
                response = ray.get(controller.evict.remote(mk, rid))
                for r in response.replicas:
                    emit(on_message, f"  ✓ {mk} [{r.replica_id}]: evicted")
                    evictions.append({"model_key": mk, "replica_id": r.replica_id})
            # Nothing to add — count already met.
            continue

        # Shortfall: add (wanted - have) replicas.
        adjusted = dict(spec)
        adjusted["replicas"] = wanted - have
        remaining[mk] = adjusted

    return evictions, remaining


# Re-export the connectivity error so callers can `from ...lib.deploy import
# NDIFConnectivityError` if they only want a single import line.
__all__ = ["deploy", "NDIFConnectivityError"]
