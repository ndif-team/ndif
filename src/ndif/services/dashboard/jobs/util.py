"""Shared utilities for the dashboard cron jobs, anchored to the dashboard's
``data_dir`` (``~/ndif_dashboard`` by default).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests


DEFAULT_DATA_DIR = Path(os.environ.get("NDIF_DASHBOARD_DATA_DIR", str(Path.home() / "ndif_dashboard")))
DEFAULT_LOG_DIR = DEFAULT_DATA_DIR / "logs"
DEFAULT_CONFIG = DEFAULT_DATA_DIR / "config.json"
DEFAULT_MAX_DAYS = 30
TIMEOUT = 10


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def send_discord(webhook_url: str, message: str) -> bool:
    try:
        resp = requests.post(webhook_url, json={"content": message}, timeout=TIMEOUT)
        if not resp.ok:
            print(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"Discord webhook failed: {e}")
        return False


_TRACEBACK_HEADER = "Traceback (most recent call last):"


def summarize_error(err: object) -> str:
    """Reduce an error to the one line worth putting in a Discord ping.

    ``str(e)`` on nnsight's ``RemoteException`` is a *rendered traceback*, not a
    message: the useful text is the trailing ``RemoteException: ...`` line and
    the first line is the literal "Traceback (most recent call last):". Taking
    line zero therefore collapsed every entry in the models_failed ping to that
    same useless header — which is all the 2026-09-08 outage ping showed. Take
    the last line for traceback-shaped strings and the first for ordinary ones;
    the full text belongs in ``models_*.log``, not the webhook.
    """
    lines = [ln.strip() for ln in str("" if err is None else err).splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return "unknown"
    return lines[-1] if lines[0].startswith(_TRACEBACK_HEADER) else lines[0]


def get_mention(config: dict) -> str:
    role_id = config.get("discord_role_id")
    return f"<@&{role_id}>" if role_id else ""


def rotate_logs(log_dir: Path, pattern: str, max_days: int) -> None:
    log_files = sorted(log_dir.glob(pattern))
    while len(log_files) > max_days:
        log_files.pop(0).unlink()
