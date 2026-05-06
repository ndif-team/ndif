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
    envoy_class: Optional[str] = None
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
    """Call an ``ndif_client`` action and translate errors into HTTP responses
    that carry a useful ``detail`` for the frontend toast.

    Without the broad ``except Exception``, anything that isn't a connectivity
    error or a ValueError (e.g. an HF ``OSError`` for a gated repo, a Ray
    actor lookup failure) bubbles up and FastAPI's default handler returns
    the bare ``{"detail": "Internal Server Error"}`` — useless to the user.
    """
    try:
        return fn(*args, **kwargs)
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("ndif_client call failed")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


def _log_action(action: str, payload, result: dict) -> None:
    """Log a per-action transcript so failures surface in ``docker logs``.

    The deploy lib returns 200 even when individual models fail (CANT_ACCOMMODATE,
    wait-for-ready timeout, etc.) — without this, the only place those errors
    show up is the toast in the user's browser, which is gone the moment they
    dismiss it. Always log the deploy_lib's progress lines + per-model status.
    """
    payload_summary = getattr(payload, "model_dump_json", lambda: str(payload))()
    deps = result.get("deployments") or []
    results = result.get("results") or []
    failed = [d for d in deps if d.get("error")] + [
        r for r in results if r.get("status") not in ("evicted", "ok", "READY", "DEPLOYED")
    ]

    if failed:
        logger.warning("[%s] %s — failures: %s", action, payload_summary, failed)
    else:
        logger.info("[%s] %s — ok", action, payload_summary)

    for line in result.get("logs") or []:
        logger.info("[%s] %s", action, line)


@router.post("/deploy")
def deploy_endpoint(payload: DeployRequest, _: str = Depends(require_auth)):
    result = _call(ndif_client.deploy, [payload.model_dump()], sync=False)
    _log_action("deploy", payload, result)
    return {"mode": "ad-hoc", **result}


@router.post("/evict")
def evict_endpoint(payload: EvictRequest, _: str = Depends(require_auth)):
    result = _call(ndif_client.evict, model_keys=[payload.model_key])
    _log_action("evict", payload, result)
    return {"mode": "ad-hoc", **result}


@router.post("/restart")
def restart_endpoint(payload: RestartRequest, _: str = Depends(require_auth)):
    result = _call(
        ndif_client.restart,
        checkpoint=payload.checkpoint,
        revision=payload.revision,
        model_key=payload.model_key,
    )
    _log_action("restart", payload, result)
    return result
