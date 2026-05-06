"""Read endpoint for the deploy autocomplete cache.

The cache itself is populated by the deploy / reconcile paths (see
``cache_store.add_many``). This router is read-only; the frontend hits it
on view mount + after every deploy to refresh its dropdowns.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import cache_store
from ..auth import require_auth
from ..config import Settings, get_settings


router = APIRouter(prefix="/api", tags=["cache"])


@router.get("/cache")
def get_cache(
    _: str = Depends(require_auth),
    settings: Settings = Depends(get_settings),
):
    return cache_store.read(settings.cache_path)
