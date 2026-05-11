"""One-off deploy / evict / status — the buttons on the admin pages.

The reconcile cron handles the scheduled set; these endpoints exist so an
admin can also push an ad-hoc model or pull up the cluster state.
"""

from __future__ import annotations

from typing import Optional

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import require_auth
from ..config import Settings, get_settings
from ..schedule_store import ScheduleStore, filter_active
from .. import cache_store, ndif_client


router = APIRouter(prefix="/api", tags=["admin"])


class ModelSpec(BaseModel):
    checkpoint: str
    revision: Optional[str] = None
    pinned: bool = False
    actor_class: Optional[str] = None
    envoy_class: Optional[str] = None
    padding_factor: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None
    # When supplied, skip the canonicalize-via-wrapper step in cli/lib/deploy
    # and use the model_key as-is. The dashboard's WARM "redeploy" path sets
    # this from the existing deployment card so we don't pay a second
    # ``get_model_key`` HF roundtrip just to recompute a key we already have.
    model_key: Optional[str] = None


class DeployRequest(BaseModel):
    specs: list[ModelSpec] = Field(min_length=1)
    sync: bool = False


class EvictRequest(BaseModel):
    model_keys: Optional[list[str]] = None
    checkpoints: Optional[list[tuple[str, Optional[str]]]] = None
    evict_all: bool = False


@router.get("/status")
def status_endpoint(
    _: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
):
    """Proxy NDIF API's /status, dedupe HF cache shadows, and tag pinned.

    Two passes:

    1. **Dedupe**: ``get_downloaded_models()`` on the controller side surfaces
       the local HF cache directory listing as COLD entries. Whenever HF was
       hit with two different casings (e.g. "Llama-3.1-8b" and "Llama-3.1-8B"),
       both directories exist on disk and both show up. If a non-COLD entry
       exists for the same case-insensitive repo id, we drop the COLD shadow.
    2. **Pinned tag**: a deployment is pinned if either (a) the controller's
       DeploymentConfig says so or (b) its ``model_key`` matches an active
       schedule entry. Schedule entries carry ``model_key`` from write-time
       canonicalization (see ``routers/schedule.py``), so this is exact
       string. The controller's True is preserved — we only OR in the
       schedule signal.
    """
    try:
        resp = requests.get(f"{settings.ndif_api_url}/status", timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach NDIF API: {e}")

    data = resp.json()

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

    # ---- Tag pinned -----------------------------------------------------
    for d in deployments.values():
        from_schedule = d.get("model_key") in pinned_keys
        d["pinned"] = bool(d.get("pinned")) or from_schedule

    return data


@router.post("/deploy")
def deploy_endpoint(
    payload: DeployRequest,
    _: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
):
    specs = [s.model_dump() for s in payload.specs]
    try:
        result = ndif_client.deploy(specs, sync=payload.sync)
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # Surface the real message (e.g. HF gated repo, controller errors)
        # so the frontend toast can show something actionable.
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

    cache_store.add_from_deploy_result(
        settings.cache_path, specs, result.get("deployments") or []
    )
    return result


@router.post("/evict")
def evict_endpoint(
    payload: EvictRequest,
    _: str = Depends(require_auth),
):
    modes = [
        bool(payload.model_keys),
        bool(payload.checkpoints),
        payload.evict_all,
    ]
    if sum(modes) != 1:
        raise HTTPException(
            status_code=400,
            detail="Exactly one of model_keys, checkpoints, evict_all",
        )

    try:
        if payload.evict_all:
            return ndif_client.evict_all()
        return ndif_client.evict(
            model_keys=payload.model_keys,
            checkpoints=payload.checkpoints,
        )
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
