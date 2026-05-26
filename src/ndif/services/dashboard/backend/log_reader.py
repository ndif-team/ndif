"""Read the JSONL log files written by ``jobs/monitor.py``.

The cron writes:
    <data_dir>/logs/connected_YYYY-MM-DD.log
    <data_dir>/logs/models_YYYY-MM-DD.log
    <data_dir>/logs/cluster_YYYY-MM-DD.log

One JSON object per line. We just concatenate, in chronological order.
"""

from __future__ import annotations

import json
from pathlib import Path


def parse_log_files(log_dir: Path, pattern: str) -> list[dict]:
    if not log_dir.exists():
        return []
    entries: list[dict] = []
    for path in sorted(log_dir.glob(pattern)):
        try:
            text = path.read_text()
        except OSError:
            continue
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return entries


def read_connected(log_dir: Path) -> list[dict]:
    return parse_log_files(log_dir, "connected_*.log")


def read_models(log_dir: Path) -> list[dict]:
    return parse_log_files(log_dir, "models_*.log")


def read_cluster(log_dir: Path) -> list[dict]:
    return parse_log_files(log_dir, "cluster_*.log")
