"""One-off deploy / evict / status — the buttons on the admin pages.

The reconcile cron handles the scheduled set; these endpoints exist so an
admin can also push an ad-hoc model or pull up the cluster state.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from ..auth import require_auth
from ..config import Settings, get_settings
from ..schedule_store import ScheduleStore, filter_active
from .. import ndif_client


router = APIRouter(prefix="/api", tags=["admin"])


@router.get("/status")
def status_endpoint(
    _: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
):
    """Fetch controller status directly via Ray, dedupe HF cache shadows,
    aggregate per-replica entries into one card per model_key, and tag pinned.

    Bypasses the NDIF API's Redis-backed status cache (``NDIF_STATUS_TTL_S``,
    default 60s). The dashboard's Deployments tab calls ``load()`` immediately
    after every deploy / evict / restart action — if we went through the cached
    HTTP path, the card you just acted on could read stale for up to that TTL
    and show the *previous* deployment_level. Hitting the controller actor
    directly costs one Ray RPC per refresh and gives instant accuracy.

    Three transforms on the payload:

    1. **Dedupe**: ``get_downloaded_models()`` on the controller side surfaces
       the local HF cache directory listing as COLD entries. Whenever HF was
       hit with two different casings (e.g. "Llama-3.1-8b" and "Llama-3.1-8B"),
       both directories exist on disk and both show up. If a non-COLD entry
       exists for the same case-insensitive repo id, we drop the COLD shadow.
    2. **Replica aggregation**: ``Controller.status()`` returns one entry per
       replica (``application_name`` = ``"{replica_id}:ModelActor:{model_key}"``).
       The Deployments UI thinks per-model, so we collapse siblings into a
       single record keyed by ``model_key`` with a ``replicas: [...]`` array.
       Card-level ``deployment_level`` / ``application_state`` / ``pinned``
       become the "best" value across siblings (HOT > WARM, RUNNING >
       DEPLOYING > NOT_STARTED > UNHEALTHY, pinned-if-any).
    3. **Pinned tag**: a deployment is pinned if either (a) the controller's
       DeploymentConfig says so or (b) its ``model_key`` matches an active
       schedule entry. Schedule entries carry ``model_key`` from write-time
       canonicalization (see ``routers/schedule.py``), so this is exact
       string. The controller's True is preserved — we only OR in the
       schedule signal.
    """
    try:
        data = ndif_client.status()
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach Ray: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    store = ScheduleStore(settings.schedule_path)
    pinned_keys = {e.model_key for e in filter_active(store.list()) if e.model_key}

    deployments: dict = data.get("deployments") or {}

    # ---- Dedupe HF cache shadows ----------------------------------------
    by_repo: dict[tuple[str, Optional[str]], list[tuple[str, dict]]] = {}
    for app_name, d in deployments.items():
        repo = d.get("repo_id")
        if not repo:
            continue
        by_repo.setdefault((repo.lower(), d.get("revision") or None), []).append((app_name, d))

    for entries in by_repo.values():
        levels = {d.get("deployment_level") for _, d in entries}
        if len(entries) > 1 and "COLD" in levels and (levels - {"COLD"}):
            for app_name, d in entries:
                if d.get("deployment_level") == "COLD":
                    deployments.pop(app_name, None)

    # ---- Aggregate per-replica entries into per-model cards -------------
    deployments = _aggregate_by_model_key(deployments)

    # ---- Tag pinned -----------------------------------------------------
    for d in deployments.values():
        from_schedule = d.get("model_key") in pinned_keys
        d["pinned"] = bool(d.get("pinned")) or from_schedule

    data["deployments"] = deployments
    return data


# Best-of priorities for collapsing per-replica entries into one card. The
# *lowest* rank wins, so HOT/RUNNING outrank WARM/DEPLOYING etc.
_LEVEL_RANK = {"HOT": 0, "WARM": 1, "COLD": 2}
_STATE_RANK = {
    "RUNNING": 0,
    "DEPLOYING": 1,
    "NOT_STARTED": 2,
    "UNHEALTHY": 3,
}


def _aggregate_by_model_key(deployments: dict) -> dict:
    """Collapse one-entry-per-replica into one-entry-per-model_key.

    HOT and WARM entries are grouped by ``model_key`` (which is uniform
    across siblings); each card carries a ``replicas`` list with the per-
    replica state. COLD entries from the HF cache listing have no
    ``model_key`` and pass through unchanged with an empty ``replicas``
    list so the frontend can treat them as single-card.

    Card-level fields choose the "best" value across replicas:
        - deployment_level: HOT > WARM > COLD
        - application_state: RUNNING > DEPLOYING > NOT_STARTED > UNHEALTHY
        - pinned: True if any replica is pinned
        - trusted: True if any replica is trusted
        - schedule: from any pinned replica that carries one
    """
    out: dict = {}

    for app_name, entry in deployments.items():
        mk = entry.get("model_key")
        if not mk:
            # COLD HF-cache shadow — pass through, keyed by app_name (which
            # is the repo_id for COLD entries from the controller).
            out[app_name] = {**entry, "replicas": []}
            continue

        replica_record = {
            "replica_id": entry.get("replica_id"),
            "deployment_level": entry.get("deployment_level"),
            "application_state": entry.get("application_state"),
            "pinned": bool(entry.get("pinned", False)),
            "trusted": bool(entry.get("trusted", False)),
        }

        card = out.get(mk)
        if card is None:
            # Seed the card from the first replica we see; subsequent ones
            # update the "best" values below.
            card = {
                "model_key": mk,
                "repo_id": entry.get("repo_id"),
                "revision": entry.get("revision"),
                "n_params": entry.get("n_params"),
                "size_bytes": entry.get("size_bytes"),
                "actor_class": entry.get("actor_class"),
                "config": entry.get("config"),
                "deployment_level": entry.get("deployment_level"),
                "application_state": entry.get("application_state"),
                "pinned": bool(entry.get("pinned", False)),
                "trusted": bool(entry.get("trusted", False)),
                "schedule": entry.get("schedule"),
                "replicas": [],
            }
            out[mk] = card

        # "Best" across siblings.
        new_level = entry.get("deployment_level")
        if (
            new_level
            and _LEVEL_RANK.get(new_level, 99)
            < _LEVEL_RANK.get(card["deployment_level"], 99)
        ):
            card["deployment_level"] = new_level

        new_state = entry.get("application_state")
        if new_state and (
            card["application_state"] is None
            or _STATE_RANK.get(new_state, 99)
            < _STATE_RANK.get(card["application_state"], 99)
        ):
            card["application_state"] = new_state

        if entry.get("pinned"):
            card["pinned"] = True
            if entry.get("schedule") and not card.get("schedule"):
                card["schedule"] = entry["schedule"]

        if entry.get("trusted"):
            card["trusted"] = True

        card["replicas"].append(replica_record)

    return out
