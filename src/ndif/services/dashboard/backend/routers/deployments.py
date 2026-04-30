"""Smart per-deployment actions for the Deployments tab.

These endpoints know about the schedule store so that admin actions on
*dedicated* models route through the schedule (preserving the invariant
that the reconcile cron is the single owner of dedicated state):

- ``POST /api/deployments/deploy`` with ``dedicated=True``  → adds an
  open-ended schedule entry (start=now, end=None) and triggers a reconcile.
  Non-dedicated → straight ``deploy`` call.
- ``POST /api/deployments/evict``  with ``dedicated=True``  → removes any
  active schedule entries matching ``(checkpoint, revision)`` and triggers
  a reconcile (which evicts via ``--sync`` semantics). Non-dedicated →
  straight ``evict`` call.
- ``POST /api/deployments/restart`` → straight ``restart`` call.

The non-smart ``/api/deploy`` and ``/api/evict`` endpoints in ``deploy.py``
are still available for callers that want raw control.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_auth
from ..config import Settings, get_settings
from ..schedule_store import ScheduleEvent, ScheduleEventIn, ScheduleStore, filter_active
from .. import ndif_client


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/deployments", tags=["deployments"])


class DeployRequest(BaseModel):
    checkpoint: str
    revision: Optional[str] = None
    actor_class: Optional[str] = None
    padding_factor: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None
    dedicated: bool = False


class EvictRequest(BaseModel):
    model_key: str
    checkpoint: Optional[str] = None
    revision: Optional[str] = None
    dedicated: bool = False


class RestartRequest(BaseModel):
    model_key: str
    checkpoint: Optional[str] = None
    revision: Optional[str] = None


def _store(settings: Settings = Depends(get_settings)) -> ScheduleStore:
    return ScheduleStore(settings.schedule_path)


def _trigger_reconcile() -> None:
    """Mirror the schedule router's behavior — kick reconcile after writes."""
    try:
        from ...jobs.reconcile import reconcile_once
        reconcile_once()
    except Exception:
        logger.exception("Background reconcile failed; the next cron tick will retry")


# ---------------------------------------------------------------------------


@router.post("/deploy")
def deploy_endpoint(
    payload: DeployRequest,
    background: BackgroundTasks,
    _: str = Depends(require_auth),
    store: ScheduleStore = Depends(_store),
):
    if payload.dedicated:
        # Dedicated == always-on schedule entry. Reconcile picks it up.
        title = f"{payload.checkpoint}"
        if payload.revision:
            title += f" @ {payload.revision}"
        title += " (deployed via dashboard)"

        event_in = ScheduleEventIn(
            title=title,
            checkpoint=payload.checkpoint,
            revision=payload.revision,
            actor_class=payload.actor_class,
            padding_factor=payload.padding_factor,
            execution_timeout_seconds=payload.execution_timeout_seconds,
            start=dt.datetime.now(dt.timezone.utc),
            end=None,
        )
        event = store.create(event_in)
        background.add_task(_trigger_reconcile)
        return {"mode": "scheduled", "event_id": event.id, "checkpoint": payload.checkpoint}

    # Non-dedicated → push straight through deploy(), no schedule churn.
    try:
        result = ndif_client.deploy(
            [payload.model_dump(exclude={"dedicated"}) | {"dedicated": False}],
            sync=False,
        )
        return {"mode": "ad-hoc", **result}
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/evict")
def evict_endpoint(
    payload: EvictRequest,
    background: BackgroundTasks,
    _: str = Depends(require_auth),
    store: ScheduleStore = Depends(_store),
):
    if payload.dedicated:
        # Remove every *active* schedule entry that matches this deployment.
        # Match on (checkpoint, revision) since that's what schedule entries
        # store and what the controller's deployment exposes via /status.
        if not payload.checkpoint:
            raise HTTPException(
                status_code=400,
                detail="checkpoint is required for dedicated evict",
            )

        deleted: list[str] = []
        for event in filter_active(store.list()):
            if event.checkpoint == payload.checkpoint and (
                (event.revision or None) == (payload.revision or None)
            ):
                if store.delete(event.id):
                    deleted.append(event.id)

        if not deleted:
            # Nothing to delete from schedule, but the deployment may still
            # exist (e.g. deployed manually before the dashboard started).
            # Fall back to a direct evict so the button isn't a no-op.
            try:
                result = ndif_client.evict(model_keys=[payload.model_key])
                return {"mode": "ad-hoc", **result}
            except ndif_client.NDIFConnectivityError as e:
                raise HTTPException(status_code=503, detail=str(e))

        background.add_task(_trigger_reconcile)
        return {"mode": "scheduled", "deleted_event_ids": deleted}

    # Non-dedicated → direct evict.
    try:
        result = ndif_client.evict(model_keys=[payload.model_key])
        return {"mode": "ad-hoc", **result}
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/restart")
def restart_endpoint(
    payload: RestartRequest,
    _: str = Depends(require_auth),
):
    try:
        return ndif_client.restart(
            checkpoint=payload.checkpoint,
            revision=payload.revision,
            model_key=payload.model_key,
        )
    except ndif_client.NDIFConnectivityError as e:
        raise HTTPException(status_code=503, detail=str(e))
