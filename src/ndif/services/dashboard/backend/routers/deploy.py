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
from .. import ndif_client


router = APIRouter(prefix="/api", tags=["admin"])


class ModelSpec(BaseModel):
    checkpoint: str
    revision: Optional[str] = None
    dedicated: bool = False
    actor_class: Optional[str] = None
    padding_factor: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None


class DeployRequest(BaseModel):
    specs: list[ModelSpec] = Field(min_length=1)
    sync: bool = False


class EvictRequest(BaseModel):
    model_keys: Optional[list[str]] = None
    checkpoints: Optional[list[tuple[str, Optional[str]]]] = None
    evict_all: bool = False
    flush_cache: bool = False


@router.get("/status")
def status_endpoint(
    _: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
):
    """Proxy NDIF API's /status (HTTP, no Ray client needed) and tag each
    deployment with ``dedicated: bool`` based on whether it matches an active
    schedule entry. The schedule store is the source of truth for "this
    model is currently pinned by the dashboard."
    """
    try:
        resp = requests.get(f"{settings.ndif_api_url}/status", timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise HTTPException(status_code=503, detail=f"Cannot reach NDIF API: {e}")

    data = resp.json()

    # Build (checkpoint, revision) → True for currently-active schedule entries.
    store = ScheduleStore(settings.schedule_path)
    active = filter_active(store.list())
    dedicated_keys = {(e.checkpoint, e.revision or None) for e in active}

    deployments = data.get("deployments") or {}
    for d in deployments.values():
        repo = d.get("repo_id")
        rev = d.get("revision") or None
        d["dedicated"] = (repo, rev) in dedicated_keys

    return data


@router.post("/deploy")
def deploy_endpoint(
    payload: DeployRequest,
    _: str = Depends(require_auth),
):
    try:
        return ndif_client.deploy(
            [s.model_dump() for s in payload.specs],
            sync=payload.sync,
        )
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/evict")
def evict_endpoint(
    payload: EvictRequest,
    _: str = Depends(require_auth),
):
    modes = [
        bool(payload.model_keys),
        bool(payload.checkpoints),
        payload.evict_all,
        payload.flush_cache,
    ]
    if sum(modes) != 1:
        raise HTTPException(
            status_code=400,
            detail="Exactly one of model_keys, checkpoints, evict_all, flush_cache",
        )

    try:
        if payload.flush_cache:
            return ndif_client.flush_warm_cache()
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
