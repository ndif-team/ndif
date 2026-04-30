#!/usr/bin/env python3
"""Reconcile the schedule store against the controller.

Replaces the pull-based ``SchedulingActor`` (Google Calendar) with a
push-based cron job:

    1. Read ``schedule.json``.
    2. Filter to events whose ``[start, end)`` window contains "now".
    3. Hash that active set; bail if unchanged from last run.
    4. Otherwise call ``deploy(specs, sync=True)`` so the controller's
       set of dedicated models matches exactly. ``sync=True`` evicts anything
       previously dedicated that's no longer scheduled.
    5. Persist the new hash + per-event ``last_status`` / ``last_error`` back
       to ``schedule.json``. Discord-notify failures.

Calendar-driven deployments are always ``dedicated=True`` (matches today's
gcal semantics).

Invoked from cron as::

    python -m ndif.services.dashboard.jobs.reconcile
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Iterable

import requests

from ..backend.config import get_settings
from ..backend.schedule_store import ScheduleEvent, ScheduleStore, filter_active
from .util import get_mention, load_config, send_discord


logger = logging.getLogger("ndif.dashboard.reconcile")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")


# ---------------------------------------------------------------------------


def _spec_from_event(e: ScheduleEvent) -> dict:
    return {
        "checkpoint": e.checkpoint,
        "revision": e.revision,
        "dedicated": True,  # calendar entries are always dedicated
        "actor_class": e.actor_class,
        "padding_factor": e.padding_factor,
        "execution_timeout_seconds": e.execution_timeout_seconds,
    }


def _hash_active(events: Iterable[ScheduleEvent]) -> str:
    """Deterministic hash of the active set so we only push on change."""
    rows = []
    for e in events:
        rows.append(json.dumps(_spec_from_event(e), sort_keys=True))
    rows.sort()
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text())
    except json.JSONDecodeError:
        return {}


def _save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def _controller_has_all_active(active: list[ScheduleEvent], api_url: str) -> bool:
    """Cheap pre-check: does the controller currently have every active
    schedule entry deployed as HOT?

    Why: the schedule-set hash is necessary but not sufficient. If NDIF
    restarts (or an admin evicts a dedicated model out-of-band), the active
    set hasn't changed but the controller has lost the deployments. Without
    this check the cron would happily skip the push, leaving the schedule
    silently unenforced.

    Conservative on errors: if /status is unreachable, return False so we
    fall through to a re-push. Better to push needlessly than to drop a
    dedicated model.
    """
    try:
        r = requests.get(f"{api_url.rstrip('/')}/status", timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Cannot verify controller state via /status: %s", e)
        return False

    deps = r.json().get("deployments") or {}
    hot_pairs = {
        (v.get("repo_id"), v.get("revision") or None)
        for v in deps.values()
        if v.get("deployment_level") == "HOT"
    }
    for ev in active:
        if (ev.checkpoint, ev.revision or None) not in hot_pairs:
            logger.info(
                "Controller missing active model %s (revision=%s); will re-push",
                ev.checkpoint, ev.revision,
            )
            return False
    return True


def _notify_failures(failed: list[dict]) -> None:
    if not failed:
        return
    settings = get_settings()
    config = load_config(settings.monitor_config_path)
    webhook = config.get("discord_webhook")
    if not webhook:
        return

    template = (
        config.get("messages", {}).get("schedule_failed")
        or "⚠️ **Scheduled deployment failed** {mention}\n{model_list}"
    )
    lines = [
        f"> **{f['checkpoint']}** — `{f['status']}`: {f.get('error', 'unknown')}"
        for f in failed
    ]
    send_discord(webhook, template.format(
        mention=get_mention(config),
        model_list="\n".join(lines),
    ))


# ---------------------------------------------------------------------------


def reconcile_once(*, force: bool = False) -> dict:
    """Run one reconcile pass. Used by cron and the FastAPI background task.

    Args:
        force: If True, push to the controller even when the active set hash
            hasn't changed. Useful after a hand-edit of ``schedule.json`` or
            when manually re-running.

    Returns:
        ``{"changed": bool, "active": [model_key, ...], "result": <deploy result> | None}``
    """
    settings = get_settings()
    store = ScheduleStore(settings.schedule_path)

    now = dt.datetime.now(dt.timezone.utc)
    events = store.list()
    active = filter_active(events, when=now)

    new_hash = _hash_active(active)
    state = _load_state(settings.reconcile_state_path)
    old_hash = state.get("hash")

    # The hash detects schedule edits. The controller-state check detects
    # drift caused by things outside the schedule (NDIF restart, admin
    # evicts a dedicated model out-of-band, controller crash, etc).
    hash_unchanged = new_hash == old_hash
    controller_in_sync = (
        not active or _controller_has_all_active(active, settings.ndif_api_url)
    )

    if hash_unchanged and controller_in_sync and not force:
        logger.info("No change (%d active event(s)); controller state matches", len(active))
        return {"changed": False, "active": [e.checkpoint for e in active], "result": None}

    if hash_unchanged and not controller_in_sync:
        logger.info("Schedule unchanged but controller drifted; re-pushing")

    logger.info("Active set changed (%d event(s)); pushing to controller", len(active))

    # Import lazily — keeps the FastAPI startup cost low.
    from ...dashboard.backend import ndif_client  # noqa: E402

    specs = [_spec_from_event(e) for e in active]

    if not specs:
        # Nothing should be dedicated — sync evicts everything currently HOT
        # if the desired set is empty. We pass an empty deploy with sync=True.
        # ``deploy()`` handles the empty-spec case via the sync branch.
        logger.info("Active set is empty; evicting any previously-dedicated models")

    try:
        result = ndif_client.deploy(specs, sync=True)
    except ndif_client.NDIFConnectivityError as e:
        logger.error("Reconcile aborted: %s", e)
        return {"changed": False, "active": [e_.checkpoint for e_ in active], "result": None,
                "error": str(e)}
    except Exception as e:
        logger.exception("Reconcile failed")
        # Don't update the hash on hard failure so the next tick will retry.
        return {"changed": False, "active": [e_.checkpoint for e_ in active], "result": None,
                "error": str(e)}

    # Echo the deploy_lib's progress lines into the cron log so admins can
    # actually see what happened — these are the same lines the CLI prints.
    for line in result.get("logs", []):
        logger.info("[deploy] %s", line)
    for d in result.get("deployments", []):
        logger.info(
            "[result] %s status=%s%s",
            d.get("checkpoint"),
            d.get("status"),
            f" error={d['error']}" if d.get("error") else "",
        )

    # Update per-event status from the deploy result, mapping by checkpoint
    by_checkpoint = {d["checkpoint"]: d for d in result.get("deployments", [])}
    failed: list[dict] = []
    for ev in active:
        d = by_checkpoint.get(ev.checkpoint)
        if d is None:
            continue
        store.mark_status(ev.id, d["status"], d.get("error"))
        if d.get("error"):
            failed.append({
                "checkpoint": ev.checkpoint,
                "status": d["status"],
                "error": d["error"],
            })

    state["hash"] = new_hash
    state["last_run"] = now.isoformat()
    state["active_count"] = len(active)
    _save_state(settings.reconcile_state_path, state)

    _notify_failures(failed)

    return {
        "changed": True,
        "active": [e.checkpoint for e in active],
        "result": result,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="Push to controller even if the active set hash is unchanged")
    args = parser.parse_args()

    out = reconcile_once(force=args.force)
    print(json.dumps({k: v for k, v in out.items() if k != "result"}, default=str))
    if out.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
