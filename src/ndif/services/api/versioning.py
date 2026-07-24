"""Optional client-version gating for the API.

The nnsight client stamps its own nnsight + python versions in the
``nnsight-version`` and ``python-version`` request headers. If a corresponding
minimum is configured via env var, requests from older (or non-reporting)
clients are rejected with 400. If a minimum is **not** set, that check is
skipped entirely — no gating.

    NDIF_MIN_NNSIGHT_VERSION   e.g. "0.5.0"   (unset -> no nnsight gating)
    NDIF_MIN_PYTHON_VERSION    e.g. "3.10"    (unset -> no python gating)

Both are read once at import (set them before the workers start, like the other
NDIF_* knobs). An empty value is treated as unset.
"""

import os
from typing import Optional

from fastapi import HTTPException
from packaging.version import InvalidVersion, Version

# `or None` so an empty env var reads as "not configured" (no gating).
MIN_NNSIGHT_VERSION: Optional[str] = os.environ.get("NDIF_MIN_NNSIGHT_VERSION") or None
MIN_PYTHON_VERSION: Optional[str] = os.environ.get("NDIF_MIN_PYTHON_VERSION") or None


def _reject(detail: str) -> None:
    raise HTTPException(status_code=400, detail=detail)


def _check_nnsight(client_version: Optional[str]) -> None:
    """Enforce ``MIN_NNSIGHT_VERSION`` against the client's nnsight version."""
    if not client_version:
        _reject(
            "Client nnsight version was not provided. This usually means an "
            "outdated nnsight; please `pip install --upgrade nnsight` and retry."
        )

    try:
        client = Version(client_version)
    except InvalidVersion:
        _reject(f"Malformed nnsight version '{client_version}'.")

    if client < Version(MIN_NNSIGHT_VERSION):
        _reject(
            f"Client nnsight version {client_version} is below the minimum "
            f"supported {MIN_NNSIGHT_VERSION}. Please "
            f"`pip install --upgrade nnsight`."
        )


def _check_python(client_version: Optional[str]) -> None:
    """Enforce ``MIN_PYTHON_VERSION`` against the client's python version.

    The client sends the full ``sys.version`` string; only major.minor is
    compared (patch/build noise is dropped).
    """
    if not client_version:
        _reject(
            "Client python version was not provided. This usually means an "
            "outdated nnsight; please `pip install --upgrade nnsight` and retry."
        )

    major_minor = ".".join(client_version.split(".")[0:2])
    try:
        client = Version(major_minor)
    except InvalidVersion:
        _reject(f"Malformed python version '{client_version}'.")

    if client < Version(MIN_PYTHON_VERSION):
        _reject(
            f"Client python version {major_minor} is below the minimum "
            f"supported {MIN_PYTHON_VERSION}. Please update python."
        )


def validate_client_versions(
    nnsight_version: Optional[str],
    python_version: Optional[str],
) -> None:
    """Gate a request on client compatibility. A no-op unless the matching
    ``NDIF_MIN_*`` env var is set. Raises 400 on an incompatible client.
    """
    if MIN_NNSIGHT_VERSION is not None:
        _check_nnsight(nnsight_version)
    if MIN_PYTHON_VERSION is not None:
        _check_python(python_version)
