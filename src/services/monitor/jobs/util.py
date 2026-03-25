"""Shared utilities for monitor scripts."""

import json
from pathlib import Path

import requests

DEFAULT_LOG_DIR = Path(__file__).parent.parent / "logs"
DEFAULT_CONFIG = Path(__file__).parent.parent / "config.json"
DEFAULT_MAX_DAYS = 30
TIMEOUT = 10


def load_config(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}


def send_discord(webhook_url: str, message: str):
    requests.post(webhook_url, json={"content": message}, timeout=TIMEOUT)


def get_mention(config: dict) -> str:
    role_id = config.get("discord_role_id")
    return f"<@&{role_id}>" if role_id else ""


def rotate_logs(log_dir: Path, pattern: str, max_days: int):
    log_files = sorted(log_dir.glob(pattern))
    while len(log_files) > max_days:
        log_files.pop(0).unlink()
