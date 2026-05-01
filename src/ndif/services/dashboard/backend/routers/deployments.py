"""Per-deployment actions for the Deployments tab.

Deploy / evict / restart are direct CLI-lib calls. ``pinned`` is just a
flag on the deployment — it tells the controller "do not evict this" but
otherwise has nothing to do with the schedule. The schedule is a separate
concern handled by the reconcile cron.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_auth
from .. import ndif_client


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


class DeployRequest(BaseModel):
    checkpoint: str
    revision: Optional[str] = None
    actor_class: Optional[str] = None
    padding_factor: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None
    pinned: bool = False


class EvictRequest(BaseModel):
    model_key: str


class RestartRequest(BaseModel):
    model_key: str
    checkpoint: Optional[str] = None
    revision: Optional[str] = None


def _call(fn: Callable, *args, **kwargs):
    """Call an ``ndif_client`` action and translate its known errors to HTTP."""
    try:
        return fn(*args, **kwargs)
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/deploy")
def deploy_endpoint(payload: DeployRequest, _: str = Depends(require_auth)):
    return {"mode": "ad-hoc", **_call(ndif_client.deploy, [payload.model_dump()], sync=False)}


@router.post("/evict")
def evict_endpoint(payload: EvictRequest, _: str = Depends(require_auth)):
    return {"mode": "ad-hoc", **_call(ndif_client.evict, model_keys=[payload.model_key])}


@router.post("/restart")
def restart_endpoint(payload: RestartRequest, _: str = Depends(require_auth)):
    return _call(
        ndif_client.restart,
        checkpoint=payload.checkpoint,
        revision=payload.revision,
        model_key=payload.model_key,
    )
