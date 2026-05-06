"""Read-only endpoints powering the Monitor view.

Each endpoint just reads the JSONL files written by ``jobs/monitor.py``.
Auth-required (every dashboard endpoint is).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ..auth import require_auth
from ..config import Settings, get_settings
from ..log_reader import read_cluster, read_connected, read_models


router = APIRouter(prefix="/api/monitor", tags=["monitor"])


@router.get("/connected")
def connected(_: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    return read_connected(settings.logs_dir)


@router.get("/models")
def models(_: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    return read_models(settings.logs_dir)


@router.get("/cluster")
def cluster(_: str = Depends(require_auth), settings: Settings = Depends(get_settings)):
    return read_cluster(settings.logs_dir)
