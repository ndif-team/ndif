#!/usr/bin/env python3
"""Reconcile the schedule store against the controller.

A push-based cron job that owns the schedule diff itself:

    1. Read ``schedule.json``.
    2. Filter to events whose ``[start, end)`` window contains "now". An
       event with ``end is None`` is open-ended ("active forever after start").
    3. Compare the new active set against (a) the previously-pushed active
       set persisted in ``.reconcile.state.json`` and (b) what the controller
       actually has HOT right now.
       - to_evict   = previously-active − new-active
       - to_deploy  = new-active − currently-HOT (covers both "newly added"
         and "drifted out — controller no longer has it")
    4. Issue explicit evict / deploy CLI-lib calls. The controller no longer
       has any "sync mode" of its own; pinned just means "do not evict".
    5. Persist the new active set + per-event status back. Discord-notify
       failures.

Schedule entries always carry ``pinned=True`` (the schedule's whole purpose
is to keep models up).

Invoked from cron as::

    python -m ndif.services.dashboard.jobs.reconcile
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import requests

from ..backend.config import get_settings
from ..backend.schedule_store import ScheduleEvent, ScheduleStore, filter_active
from .util import get_mention, load_config, send_discord


logger = logging.getLogger("ndif.dashboard.reconcile")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spec_from_event(e: ScheduleEvent) -> dict:
    return {
        "checkpoint": e.checkpoint,
        "revision": e.revision,
        "pinned": True,  # schedule entries are always pinned
        "actor_class": e.actor_class,
        "padding_factor": e.padding_factor,
        "execution_timeout_seconds": e.execution_timeout_seconds,
    }


def _pair(spec_or_event) -> tuple[str, Optional[str]]:
    """The (checkpoint, revision) pair that identifies a deployment.

    Schedule entries store ``revision``; ``/status`` deployments expose
    ``repo_id`` and ``revision``. We compare on ``(checkpoint, revision or None)``
    so the absent and explicit-null revisions match.
    """
    if isinstance(spec_or_event, ScheduleEvent):
        return (spec_or_event.checkpoint, spec_or_event.revision or None)
    return (spec_or_event["checkpoint"], spec_or_event.get("revision") or None)


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


def _fetch_hot_pairs(api_url: str) -> Optional[set[tuple[str, Optional[str]]]]:
    """Return the set of (repo_id, revision) currently HOT according to the
    NDIF API's HTTP /status. ``None`` on transport error → callers should
    treat that as "unknown" and lean toward re-deploying.
    """
    try:
        r = requests.get(f"{api_url.rstrip('/')}/status", timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Cannot read /status to verify controller: %s", e)
        return None

    deps = r.json().get("deployments") or {}
    return {
        (v.get("repo_id"), v.get("revision") or None)
        for v in deps.values()
        if v.get("deployment_level") == "HOT"
    }


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
# main entry
# ---------------------------------------------------------------------------


def reconcile_once(*, force: bool = False) -> dict:
    """Run one reconcile pass. Used by cron and by the FastAPI background
    task that fires after a schedule write.

    Args:
        force: If True, treat every active entry as needing a re-deploy and
            every disappeared entry as needing eviction, even if the
            persisted state and controller agree.

    Returns:
        ``{"changed": bool, "active": [...], "evicted": [...], "deployed": [...]}``
    """
    settings = get_settings()
    store = ScheduleStore(settings.schedule_path)

    now = dt.datetime.now(dt.timezone.utc)
    events = store.list()
    active = filter_active(events, when=now)

    state = _load_state(settings.reconcile_state_path)
    prev_specs: list[dict] = state.get("active_specs", [])
    prev_pairs = {_pair(s) for s in prev_specs}

    new_specs = [_spec_from_event(e) for e in active]
    new_pairs = {_pair(s) for s in new_specs}

    # Models we previously had pinned but no longer want pinned.
    to_evict_pairs = sorted(prev_pairs - new_pairs)

    # Verify the controller still has every active model HOT.  Anything in
    # the active set that ISN'T currently HOT needs to be (re-)deployed.
    hot_pairs = _fetch_hot_pairs(settings.ndif_api_url)
    to_deploy_specs: list[dict] = []
    for spec in new_specs:
        p = _pair(spec)
        if force:
            to_deploy_specs.append(spec)
        elif p not in prev_pairs:
            # newly added to schedule
            to_deploy_specs.append(spec)
        elif hot_pairs is None or p not in hot_pairs:
            # drifted out, or we couldn't verify — be safe, re-push
            to_deploy_specs.append(spec)

    if not to_evict_pairs and not to_deploy_specs:
        logger.info(
            "No change (%d active event(s)); controller in sync", len(active)
        )
        # Re-persist active_specs in case anything changed in the spec body
        # (revision, padding_factor, etc.) without changing the (cp, rev) pair.
        state["active_specs"] = new_specs
        state["last_run"] = now.isoformat()
        state["active_count"] = len(active)
        _save_state(settings.reconcile_state_path, state)
        return {
            "changed": False,
            "active": [e.checkpoint for e in active],
            "evicted": [],
            "deployed": [],
        }

    logger.info(
        "Reconciling: evict=%d deploy=%d (active=%d)",
        len(to_evict_pairs), len(to_deploy_specs), len(active),
    )

    # Import lazily — keeps the FastAPI startup cost low.
    from ..backend import ndif_client

    failed: list[dict] = []
    evicted_pairs: list[tuple] = []
    deployed_pairs: list[tuple] = []

    # ---- Evict removed entries -----------------------------------------
    if to_evict_pairs:
        try:
            r = ndif_client.evict(checkpoints=[(cp, rev) for cp, rev in to_evict_pairs])
            for line in r.get("logs", []):
                logger.info("[evict] %s", line)
            for entry in r.get("results", []):
                logger.info(
                    "[evict-result] %s status=%s",
                    entry.get("model_key"), entry.get("status"),
                )
            evicted_pairs = list(to_evict_pairs)
        except ndif_client.NDIFConnectivityError as e:
            logger.error("Evict step aborted: %s", e)
            return {
                "changed": False,
                "active": [e.checkpoint for e in active],
                "evicted": [], "deployed": [],
                "error": str(e),
            }
        except Exception as e:
            logger.exception("Evict step failed")
            return {
                "changed": False,
                "active": [e_.checkpoint for e_ in active],
                "evicted": [], "deployed": [],
                "error": str(e),
            }

    # ---- Deploy added / drifted entries --------------------------------
    deploy_result: dict = {}
    if to_deploy_specs:
        try:
            deploy_result = ndif_client.deploy(to_deploy_specs, sync=False)
            for line in deploy_result.get("logs", []):
                logger.info("[deploy] %s", line)
            for d in deploy_result.get("deployments", []):
                logger.info(
                    "[deploy-result] %s status=%s%s",
                    d.get("checkpoint"), d.get("status"),
                    f" error={d['error']}" if d.get("error") else "",
                )
            deployed_pairs = [_pair(s) for s in to_deploy_specs]
        except ndif_client.NDIFConnectivityError as e:
            logger.error("Deploy step aborted: %s", e)
            # Eviction already happened; persist that progress so we don't
            # try to evict again next tick.
            state["active_specs"] = new_specs
            state["last_run"] = now.isoformat()
            state["active_count"] = len(active)
            _save_state(settings.reconcile_state_path, state)
            return {
                "changed": True,
                "active": [e.checkpoint for e in active],
                "evicted": [list(p) for p in evicted_pairs],
                "deployed": [],
                "error": str(e),
            }
        except Exception as e:
            logger.exception("Deploy step failed")
            state["active_specs"] = new_specs
            state["last_run"] = now.isoformat()
            state["active_count"] = len(active)
            _save_state(settings.reconcile_state_path, state)
            return {
                "changed": True,
                "active": [e_.checkpoint for e_ in active],
                "evicted": [list(p) for p in evicted_pairs],
                "deployed": [],
                "error": str(e),
            }

    # Update per-event status from the deploy result
    by_checkpoint = {d["checkpoint"]: d for d in deploy_result.get("deployments", [])}
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

    state["active_specs"] = new_specs
    state["last_run"] = now.isoformat()
    state["active_count"] = len(active)
    _save_state(settings.reconcile_state_path, state)

    _notify_failures(failed)

    return {
        "changed": True,
        "active": [e.checkpoint for e in active],
        "evicted": [list(p) for p in evicted_pairs],
        "deployed": [list(p) for p in deployed_pairs],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force", action="store_true",
        help="Re-push every active entry, even if state agrees",
    )
    args = parser.parse_args()

    out = reconcile_once(force=args.force)
    print(json.dumps(out, default=str))
    if out.get("error"):
        sys.exit(1)


if __name__ == "__main__":
    main()
