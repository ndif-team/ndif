#!/usr/bin/env python3
"""Reconcile the schedule store against the controller.

A push-based cron job that owns the schedule diff itself:

    1. Read ``schedule.json``.
    2. Filter to events whose ``[start, end)`` window contains "now". An
       event with ``end is None`` is open-ended ("active forever after start").
    3. Diff against (a) the previously-pushed model_keys persisted in
       ``.reconcile.state.json`` and (b) what the controller has HOT now,
       including whether each HOT replica is actually pinned.
       - to_evict   = (previously-active − new-active) + pinned-drift
       - to_deploy  = new-active − currently-HOT-and-pinned (covers "newly
         added", "drifted out — controller no longer has it", and "HOT but
         unpinned" — a scheduled model whose pinned replica died and was
         re-created unpinned by a hotswap request, which then rejects
         non-hotswap keys)
    4. Issue explicit evict / deploy CLI-lib calls. The controller no longer
       has any "sync mode" of its own; pinned just means "do not evict".
    5. Persist the new active set + per-event status back. Discord-notify
       failures.

Identity is the nnsight ``model_key`` — schedule entries get one stamped on
write (see ``routers/schedule.py::_canonicalize``) and so do controller
deployments via ``/status``. Comparison is exact-string.

Schedule entries always carry ``pinned=True`` (the schedule's whole purpose
is to keep models up).

Invoked from cron as::

    python -m ndif.services.dashboard.jobs.reconcile
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import logging
import sys
from pathlib import Path
from typing import Optional

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
        "envoy_class": e.envoy_class,
        "padding_factor": e.padding_factor,
        "execution_timeout_seconds": e.execution_timeout_seconds,
    }


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


def _fetch_hot_deployments() -> Optional[dict[str, bool]]:
    """Return ``{model_key: fully_pinned}`` for every model_key with a HOT
    replica, where ``fully_pinned`` is True only if *every* HOT replica of
    that model_key is pinned.

    Reads live from the controller via Ray rather than the API service's
    Redis-cached ``/status`` (TTL ``NDIF_STATUS_CACHE_FREQ_S``). The cached
    path was racey: during a brief actor restart or before the cache
    refreshed, a pinned model would falsely look like it had drifted out
    of HOT, and the additive deploy below would stack another pinned
    replica on top of the one already serving. ``None`` on Ray-side error
    → callers treat as "unknown" and lean toward re-deploying.

    Tracking pinned-ness (not just HOT-ness) is what lets reconcile catch a
    *scheduled* model that came back HOT but unpinned — e.g. its pinned
    replica died and a hotswap request re-created it unpinned. That model
    passes a bare "is it HOT?" test yet still rejects non-hotswap keys, so
    it must be re-pinned.
    """
    from ..backend import ndif_client

    try:
        data = ndif_client.status()
    except Exception as e:
        logger.warning("Cannot read controller status: %s", e)
        return None

    deps = data.get("deployments") or {}
    hot: dict[str, bool] = {}
    for v in deps.values():
        if v.get("deployment_level") != "HOT":
            continue
        mk = v.get("model_key")
        if not mk:
            continue
        # Fully pinned only if no HOT replica of this model_key is unpinned.
        hot[mk] = hot.get(mk, True) and bool(v.get("pinned"))
    return hot


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


def _persist_state(
    state_path: Path,
    *,
    model_keys: list[str],
    active_count: int,
) -> None:
    _save_state(state_path, {
        "prev_model_keys": model_keys,
        "active_count": active_count,
        "last_run": dt.datetime.now(dt.timezone.utc).isoformat(),
    })


@contextlib.contextmanager
def _reconcile_lock(state_path: Path):
    """Serialize concurrent reconciles via flock on a sidecar lockfile.

    Multiple FastAPI ``BackgroundTasks`` (one per schedule write) and the
    every-2-min cron can otherwise interleave: each reads stale state,
    computes its diff against it, and the last save wins — leaking models
    or losing evictions. A blocking exclusive lock around the whole
    read-diff-act-write sequence makes them serialize cleanly. Each pass
    is bounded by the deploy/evict it runs, typically <30s.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with open(lock_path, "a+") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)


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
    with _reconcile_lock(settings.reconcile_state_path):
        return _reconcile_locked(settings, force=force)


def _reconcile_locked(settings, *, force: bool) -> dict:
    store = ScheduleStore(settings.schedule_path)

    events = store.list()
    active = filter_active(events)
    new_keys: dict[str, ScheduleEvent] = {e.model_key: e for e in active}

    state = _load_state(settings.reconcile_state_path)
    prev_keys: set[str] = set(state.get("prev_model_keys") or [])

    # Live controller view: {model_key: every HOT replica pinned?} (or None).
    hot = _fetch_hot_deployments()
    new_keys_list = list(new_keys.keys())
    active_count = len(active)
    active_names = [e.checkpoint for e in active]

    # If we couldn't read controller state, skip this tick and let the next
    # cron run retry. Acting blind would stack pinned replicas: every active
    # scheduled model would look like it had drifted out of HOT and get an
    # additive pinned deploy on top of the one already serving. ``force``
    # still bypasses since it's a deliberate operator override.
    if hot is None and not force:
        logger.warning(
            "Skipping reconcile: controller status unavailable; will retry next tick"
        )
        return {
            "changed": False, "active": active_names,
            "evicted": [], "deployed": [],
            "skipped": "controller_status_unavailable",
        }

    # Pinned-drift: a scheduled model that is HOT but has an unpinned replica
    # (its pinned replica died and a hotswap request re-created it unpinned —
    # it serves hotswap keys but rejects normal ones). An additive deploy
    # can't flip an existing replica's pinned flag, and the API gate reads
    # replicas[0].pinned, so we must evict every replica and redeploy clean
    # with pinned=True. These join both the evict set and the redeploy set.
    drifted_unpinned = (
        {mk for mk in new_keys if hot.get(mk) is False}
        if hot is not None else set()
    )
    if drifted_unpinned:
        logger.warning(
            "Pinned-drift detected (scheduled but HOT-unpinned); re-pinning: %s",
            sorted(drifted_unpinned),
        )

    # to_evict: removed-from-schedule, plus pinned-drift models we must rebuild.
    to_evict = sorted((prev_keys - set(new_keys)) | drifted_unpinned)

    # to_deploy: newly added OR drifted out of HOT OR HOT-but-unpinned.
    if force:
        to_deploy_events = list(new_keys.values())
    else:
        to_deploy_events = [
            ev for mk, ev in new_keys.items()
            if mk not in prev_keys or mk not in hot or mk in drifted_unpinned
        ]

    if not to_evict and not to_deploy_events:
        logger.info("No change (%d active event(s)); controller in sync", active_count)
        _persist_state(settings.reconcile_state_path,
                       model_keys=new_keys_list, active_count=active_count)
        return {"changed": False, "active": active_names, "evicted": [], "deployed": []}

    logger.info(
        "Reconciling: evict=%d deploy=%d (active=%d)",
        len(to_evict), len(to_deploy_events), active_count,
    )

    # Lazy import — keeps the FastAPI startup cost low.
    from ..backend import ndif_client

    evicted: list[str] = []
    failed: list[dict] = []

    # ---- Evict removed entries -----------------------------------------
    if to_evict:
        try:
            r = ndif_client.evict(model_keys=to_evict)
        except Exception as e:
            level = "error" if isinstance(e, ndif_client.NDIFConnectivityError) else "exception"
            getattr(logger, level)("Evict step failed: %s", e)
            return {
                "changed": False, "active": active_names,
                "evicted": [], "deployed": [], "error": str(e),
            }
        for line in r.get("logs", []):
            logger.info("[evict] %s", line)
        for entry in r.get("results", []):
            logger.info(
                "[evict-result] %s status=%s",
                entry.get("model_key"), entry.get("status"),
            )
        evicted = list(to_evict)

    # ---- Deploy added / drifted entries --------------------------------
    deploy_result: dict = {}
    deploy_specs: list[dict] = []
    if to_deploy_events:
        deploy_specs = [_spec_from_event(e) for e in to_deploy_events]
        try:
            deploy_result = ndif_client.deploy(deploy_specs, sync=False)
        except Exception as e:
            level = "error" if isinstance(e, ndif_client.NDIFConnectivityError) else "exception"
            getattr(logger, level)("Deploy step failed: %s", e)
            # Eviction already happened — persist so we don't try to evict
            # the same models again next tick.
            _persist_state(settings.reconcile_state_path,
                           model_keys=new_keys_list, active_count=active_count)
            return {
                "changed": True, "active": active_names,
                "evicted": evicted, "deployed": [], "error": str(e),
            }
        for line in deploy_result.get("logs", []):
            logger.info("[deploy] %s", line)
        for d in deploy_result.get("deployments", []):
            logger.info(
                "[deploy-result] %s status=%s%s",
                d.get("checkpoint"), d.get("status"),
                f" error={d['error']}" if d.get("error") else "",
            )

    # Update per-event status from the deploy result, then notify
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

    _persist_state(settings.reconcile_state_path,
                   model_keys=new_keys_list, active_count=active_count)
    _notify_failures(failed)

    # Mirror successful schedule-driven deploys into the autocomplete cache,
    # same as ad-hoc deploys do via the deployments router.
    if deploy_specs:
        from ..backend import cache_store
        cache_store.add_from_deploy_result(
            settings.cache_path, deploy_specs, deploy_result.get("deployments") or [],
        )

    deployed = [e.model_key for e in to_deploy_events]
    return {
        "changed": True, "active": active_names, "evicted": evicted,
        "deployed": deployed, "repinned": sorted(drifted_unpinned),
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
