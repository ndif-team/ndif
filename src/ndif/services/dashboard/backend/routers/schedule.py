"""Schedule CRUD.

Every write triggers an immediate reconcile in a background task so the user
sees their change take effect without waiting for the next cron tick.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status

from ..auth import require_auth
from ..config import Settings, get_settings
from ..schedule_store import ScheduleEvent, ScheduleEventIn, ScheduleStore


def _canonicalize(payload: ScheduleEventIn) -> ScheduleEventIn:
    """Resolve user input via HF and stamp canonical fields onto the payload.

    Lazy-imports nnsight via ``cli/lib/util.canonicalize_checkpoint``; first
    call may take ~1-2s. Surfaces lookup failures as HTTP 400 so a typo'd
    repo gets caught at write time instead of silently failing on the next
    reconcile tick. Persisting the ``model_key`` here means everything
    downstream — reconcile diff, /api/status pinned tag, evict — can use
    exact-string ``model_key`` comparison.
    """
    from .....cli.lib.util import canonicalize_checkpoint

    try:
        canon_cp, canon_rev, model_key = canonicalize_checkpoint(
            payload.checkpoint, payload.revision
        )
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve {payload.checkpoint!r} on HuggingFace: {e}",
        )
    return payload.model_copy(update={
        "checkpoint": canon_cp,
        "revision": canon_rev,
        "model_key": model_key,
    })


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/schedule", tags=["schedule"])


def _store(settings: Settings = Depends(get_settings)) -> ScheduleStore:
    return ScheduleStore(settings.schedule_path)


def _trigger_reconcile() -> None:
    """Best-effort: kick the reconcile job after a write.

    Imported lazily so backend startup doesn't pay the cost of importing
    ``ray`` / ``nnsight`` until a write actually happens.
    """
    try:
        from ...jobs.reconcile import reconcile_once
        reconcile_once()
    except Exception:
        logger.exception("Background reconcile failed; the next cron tick will retry")


@router.get("", response_model=list[ScheduleEvent])
def list_events(_: str = Depends(require_auth), store: ScheduleStore = Depends(_store)):
    return store.list()


@router.post("", response_model=ScheduleEvent, status_code=status.HTTP_201_CREATED)
def create_event(
    payload: ScheduleEventIn,
    background: BackgroundTasks,
    _: str = Depends(require_auth),
    store: ScheduleStore = Depends(_store),
):
    event = store.create(_canonicalize(payload))
    background.add_task(_trigger_reconcile)
    return event


@router.get("/{event_id}", response_model=ScheduleEvent)
def get_event(
    event_id: str,
    _: str = Depends(require_auth),
    store: ScheduleStore = Depends(_store),
):
    event = store.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


@router.put("/{event_id}", response_model=ScheduleEvent)
def update_event(
    event_id: str,
    payload: ScheduleEventIn,
    background: BackgroundTasks,
    _: str = Depends(require_auth),
    store: ScheduleStore = Depends(_store),
):
    event = store.update(event_id, _canonicalize(payload))
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    background.add_task(_trigger_reconcile)
    return event


@router.delete("/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(
    event_id: str,
    background: BackgroundTasks,
    _: str = Depends(require_auth),
    store: ScheduleStore = Depends(_store),
):
    if not store.delete(event_id):
        raise HTTPException(status_code=404, detail="Event not found")
    background.add_task(_trigger_reconcile)
