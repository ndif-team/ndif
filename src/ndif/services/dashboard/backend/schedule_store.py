"""JSON-backed schedule store.

Schema (``schedule.json``)
--------------------------
::

    {
      "events": [
        {
          "id": "uuid-string",
          "title": "Llama 3.1 8B (research week)",
          "checkpoint": "meta-llama/Llama-3.1-8B",
          "revision": null,
          "actor_class": null,
          "padding_factor": null,
          "execution_timeout_seconds": null,
          "start": "2026-05-01T00:00:00+00:00",
          "end":   "2026-05-08T00:00:00+00:00",
          "created_at": "...",
          "updated_at": "...",
          "last_status": null,
          "last_error": null
        }
      ]
    }

All writes are atomic (``tempfile + os.replace``) and serialized through an
``fcntl.flock`` so the FastAPI workers and the reconcile cron can't clobber
each other.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Iterable, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Pydantic models — used by both the store and the FastAPI router
# ---------------------------------------------------------------------------


class ScheduleEventIn(BaseModel):
    """Payload for create/update.

    ``end`` is optional: an event with ``end is None`` is open-ended (active
    forever after ``start``). Open-ended events are how the Deployments page
    represents "make this dedicated" without forcing the admin to pick a
    sunset time.
    """

    title: str
    checkpoint: str
    start: dt.datetime
    end: Optional[dt.datetime] = None
    revision: Optional[str] = None
    actor_class: Optional[str] = None
    padding_factor: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: Optional[dt.datetime], info) -> Optional[dt.datetime]:
        if v is None:
            return v
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("end must be after start")
        return v


class ScheduleEvent(ScheduleEventIn):
    id: str
    created_at: dt.datetime
    updated_at: dt.datetime
    last_status: Optional[str] = None
    last_error: Optional[str] = None


# ---------------------------------------------------------------------------
# File store
# ---------------------------------------------------------------------------


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


@contextlib.contextmanager
def _locked(path: Path):
    """Acquire an exclusive flock on a sidecar lockfile.

    A sidecar (``<path>.lock``) is used instead of the data file itself so the
    lock survives the atomic rename done by ``_write_json``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+") as lock_fp:
        fcntl.flock(lock_fp, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {"events": []}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"events": []}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise


class ScheduleStore:
    """File-backed CRUD over ``ScheduleEvent``s."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # --- read ----------------------------------------------------------------
    def list(self) -> list[ScheduleEvent]:
        with _locked(self.path):
            data = _read_json(self.path)
        return [ScheduleEvent.model_validate(e) for e in data.get("events", [])]

    def get(self, event_id: str) -> Optional[ScheduleEvent]:
        for e in self.list():
            if e.id == event_id:
                return e
        return None

    # --- write ---------------------------------------------------------------
    def create(self, payload: ScheduleEventIn) -> ScheduleEvent:
        now = _now()
        event = ScheduleEvent(
            id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            **payload.model_dump(),
        )
        with _locked(self.path):
            data = _read_json(self.path)
            data.setdefault("events", []).append(json.loads(event.model_dump_json()))
            _write_json(self.path, data)
        return event

    def update(self, event_id: str, payload: ScheduleEventIn) -> Optional[ScheduleEvent]:
        with _locked(self.path):
            data = _read_json(self.path)
            for i, raw in enumerate(data.get("events", [])):
                if raw.get("id") == event_id:
                    existing = ScheduleEvent.model_validate(raw)
                    updated = existing.model_copy(update={
                        **payload.model_dump(),
                        "updated_at": _now(),
                    })
                    data["events"][i] = json.loads(updated.model_dump_json())
                    _write_json(self.path, data)
                    return updated
            return None

    def delete(self, event_id: str) -> bool:
        with _locked(self.path):
            data = _read_json(self.path)
            events = data.get("events", [])
            kept = [e for e in events if e.get("id") != event_id]
            if len(kept) == len(events):
                return False
            data["events"] = kept
            _write_json(self.path, data)
            return True

    def mark_status(self, event_id: str, status: str, error: Optional[str] = None) -> None:
        with _locked(self.path):
            data = _read_json(self.path)
            for raw in data.get("events", []):
                if raw.get("id") == event_id:
                    raw["last_status"] = status
                    raw["last_error"] = error
                    raw["updated_at"] = _now().isoformat()
                    break
            _write_json(self.path, data)

    # --- queries -------------------------------------------------------------
    def active_at(self, when: Optional[dt.datetime] = None) -> list[ScheduleEvent]:
        return filter_active(self.list(), when=when)


def _is_active(e: "ScheduleEvent", when: dt.datetime) -> bool:
    if e.start > when:
        return False
    if e.end is None:
        return True
    return when < e.end


def filter_active(events: Iterable[ScheduleEvent], when: Optional[dt.datetime] = None) -> list[ScheduleEvent]:
    when = when or _now()
    return [e for e in events if _is_active(e, when)]
