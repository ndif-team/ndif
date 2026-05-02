#!/usr/bin/env python3
"""Connectivity + model-trace monitor cron.

Behaviorally identical to ``services/monitor/jobs/monitor.py``; only the
default paths differ (``NDIF_DASHBOARD_DATA_DIR`` / ``~/ndif_dashboard``) so the
dashboard backend can read the same JSONL log files this script writes.

Invoked from cron as::

    python -m ndif.services.dashboard.jobs.monitor
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import fcntl
import json
import os
import signal
import sys
import time
from pathlib import Path

import requests

from .util import (
    DEFAULT_CONFIG,
    DEFAULT_LOG_DIR,
    DEFAULT_MAX_DAYS,
    TIMEOUT,
    get_mention,
    load_config,
    rotate_logs,
    send_discord,
)


DEFAULT_URL = "https://api.ndif.us"
DEFAULT_MODEL_TIMEOUT = 60
DEFAULT_MODEL_INTERVAL = 7200  # 2 hours

DEFAULT_MESSAGES = {
    "down": "🔴 **NDIF is DOWN** — {reason} at {timestamp} {mention}",
    "still_down": "🔴 **NDIF is still down** — {reason} at {timestamp} (down since {down_since})",
    "back_up": "🟢 **NDIF is back UP** — Recovered at {timestamp} (was down since {down_since}) {mention}",
    "models_failed": "⚠️ **{failed_count}/{total} model(s) failed** {mention}\n{model_list}",
}


# ---- Checks ----

def check_connected(base_url: str) -> tuple[bool, str]:
    resp = requests.get(f"{base_url}/connected", timeout=TIMEOUT)
    if resp.status_code == 200:
        return True, "ok"
    if resp.status_code == 503:
        return False, "service disconnected (503)"
    return False, f"unexpected status {resp.status_code}"


def get_status(base_url: str) -> dict:
    resp = requests.get(f"{base_url}/status", timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def extract_hot_models(status: dict) -> list[dict]:
    deployments = status.get("deployments", {})
    return [m for m in deployments.values() if m.get("deployment_level") == "HOT"]


def extract_cluster_info(status: dict) -> dict:
    cluster = status.get("cluster", {})
    nodes = cluster.get("nodes", {})
    total_gpus = 0
    total_mem = 0
    available_mem = 0
    node_details = []

    for node_id, node in nodes.items():
        gpus = node.get("resources", {}).get("gpu_details", [])
        n_gpus = len(gpus)
        node_mem = sum(g.get("memory_bytes", 0) for g in gpus)
        node_avail = sum(g.get("available_memory_bytes", 0) for g in gpus)

        deployed = []
        for key in node.get("deployments", {}):
            try:
                json_part = key.split(":", 2)[2]
                repo_id = json.loads(json_part).get("repo_id", key)
            except (IndexError, json.JSONDecodeError):
                repo_id = key
            deployed.append(repo_id)

        node_details.append({
            "node_id": node_id[:8],
            "gpus": n_gpus,
            "memory_bytes": node_mem,
            "available_bytes": node_avail,
            "deployments": deployed,
        })

        total_gpus += n_gpus
        total_mem += node_mem
        available_mem += node_avail

    return {
        "nodes": len(nodes),
        "total_gpus": total_gpus,
        "total_memory_bytes": total_mem,
        "available_memory_bytes": available_mem,
        "node_details": sorted(node_details, key=lambda n: n["node_id"]),
    }


def _run_trace(repo_id: str, api_key: str) -> dict:
    from nnsight import LanguageModel, CONFIG

    CONFIG.API.APIKEY = api_key
    result = {"model": repo_id}

    try:
        model = LanguageModel(repo_id)
    except Exception as e:
        result["status"] = "load_error"
        result["error"] = str(e)
        return result

    start = time.perf_counter()
    try:
        with model.trace("Hello", remote=True):
            output = model.output.save()
        elapsed = time.perf_counter() - start
        result["status"] = "ok"
        result["latency_s"] = round(elapsed, 3)
    except Exception as e:
        elapsed = time.perf_counter() - start
        result["status"] = "error"
        result["latency_s"] = round(elapsed, 3)
        result["error"] = str(e)

    return result


def check_model(repo_id: str, api_key: str, model_timeout: int) -> dict:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run_trace, repo_id, api_key)
    try:
        return future.result(timeout=model_timeout)
    except concurrent.futures.TimeoutError:
        return {"model": repo_id, "status": "timeout", "error": f"Exceeded {model_timeout}s timeout"}
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


# ---- State ----

def load_state(log_dir: Path) -> dict:
    state_file = log_dir / ".state.json"
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {"last_status": "ok", "down_since": None, "last_model_check": None}


def save_state(log_dir: Path, state: dict) -> None:
    with open(log_dir / ".state.json", "w") as f:
        json.dump(state, f)


# ---- Notifications ----

def format_discord_ts(iso_timestamp: str) -> str:
    dt = datetime.datetime.fromisoformat(iso_timestamp)
    unix = int(dt.timestamp())
    return f"<t:{unix}:f>"


def notify_status(config: dict, was_ok: bool, down_since: str | None, is_ok: bool, reason: str, timestamp: str) -> None:
    webhook_url = config.get("discord_webhook")
    if not webhook_url:
        return
    messages = {**DEFAULT_MESSAGES, **config.get("messages", {})}
    mention = get_mention(config)
    fmt = {
        "reason": reason,
        "timestamp": format_discord_ts(timestamp),
        "down_since": format_discord_ts(down_since) if down_since else "unknown",
        "mention": mention,
    }
    if was_ok and not is_ok:
        send_discord(webhook_url, messages["down"].format(**fmt))
    elif not was_ok and not is_ok:
        send_discord(webhook_url, messages["still_down"].format(**fmt))
    elif not was_ok and is_ok:
        send_discord(webhook_url, messages["back_up"].format(**fmt))


def notify_model_failures(config: dict, failed: list, total: int) -> None:
    webhook_url = config.get("discord_webhook")
    if not webhook_url or not failed:
        return
    messages = {**DEFAULT_MESSAGES, **config.get("messages", {})}
    mention = get_mention(config)
    lines = [f"> **{r['model']}** — `{r['status']}`: {r.get('error', 'unknown')}" for r in failed]
    send_discord(webhook_url, messages["models_failed"].format(
        failed_count=len(failed), total=total,
        mention=mention, model_list="\n".join(lines),
    ))


# ---- Main ----

SCRIPT_TIMEOUT = 480


def _timeout_handler(signum, frame):
    print("Monitor script timed out, force exiting")
    os._exit(2)


def main():
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(SCRIPT_TIMEOUT)

    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--model-timeout", type=int, default=DEFAULT_MODEL_TIMEOUT)
    parser.add_argument("--model-interval", type=int, default=DEFAULT_MODEL_INTERVAL)
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("NDIF_API_KEY")
    config = load_config(Path(args.config))
    if not api_key:
        api_key = config.get("ndif_api_key")

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    lock_file = open(log_dir / ".monitor.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another monitor instance is running, skipping")
        sys.exit(0)

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    today = datetime.date.today().isoformat()
    state = load_state(log_dir)

    is_ok = True
    reason = "ok"

    try:
        is_ok, reason = check_connected(args.url)
    except requests.RequestException:
        is_ok = False
        reason = "API unreachable"

    model_check_due = False
    if is_ok:
        if state["last_status"] != "ok":
            model_check_due = True
        else:
            last_mc = state.get("last_model_check")
            if last_mc is None:
                model_check_due = True
            else:
                elapsed = (datetime.datetime.fromisoformat(timestamp) -
                           datetime.datetime.fromisoformat(last_mc)).total_seconds()
                model_check_due = elapsed >= args.model_interval

    if is_ok and model_check_due:
        try:
            status_data = get_status(args.url)
            hot_models = extract_hot_models(status_data)

            cluster_info = extract_cluster_info(status_data)
            cluster_info["timestamp"] = timestamp
            with open(log_dir / f"cluster_{today}.log", "a") as f:
                f.write(json.dumps(cluster_info) + "\n")
        except Exception:
            is_ok = False
            reason = "/status unreachable"
            hot_models = []

        if is_ok and len(hot_models) == 0:
            is_ok = False
            reason = "no HOT models deployed"

        if is_ok:
            state["last_model_check"] = timestamp

            if not api_key:
                print("Warning: no API key set, skipping model traces")
            else:
                results = []
                for m in hot_models:
                    repo_id = m.get("repo_id", "unknown")
                    print(f"Checking {repo_id}...", flush=True)
                    result = check_model(repo_id, api_key, args.model_timeout)
                    results.append(result)
                    print(f"  {result['status']} ({result.get('latency_s', 'N/A')}s)")

                ok_count = sum(1 for r in results if r["status"] == "ok")
                total = len(results)
                model_entry = {
                    "timestamp": timestamp,
                    "status": "ok" if ok_count == total else "degraded",
                    "ok": ok_count, "total": total,
                    "results": results,
                }
                with open(log_dir / f"models_{today}.log", "a") as f:
                    f.write(json.dumps(model_entry) + "\n")

                failed = [r for r in results if r["status"] != "ok"]
                failed_set = sorted(r["model"] for r in failed)
                prev_failed = state.get("last_failed_models", [])
                if failed and failed_set != prev_failed:
                    notify_model_failures(config, failed, total)
                state["last_failed_models"] = failed_set

    connected_entry = {"timestamp": timestamp, "status": "ok" if is_ok else reason}
    with open(log_dir / f"connected_{today}.log", "a") as f:
        f.write(json.dumps(connected_entry) + "\n")

    was_ok = state["last_status"] == "ok"
    notify_down_since = timestamp if (not is_ok and was_ok) else state.get("down_since")
    if not is_ok and was_ok:
        state["down_since"] = timestamp
    elif is_ok:
        state["down_since"] = None
    state["last_status"] = "ok" if is_ok else "down"
    save_state(log_dir, state)

    notify_status(config, was_ok, notify_down_since, is_ok, reason, timestamp)

    rotate_logs(log_dir, "connected_*.log", args.max_days)
    rotate_logs(log_dir, "models_*.log", args.max_days)
    rotate_logs(log_dir, "cluster_*.log", args.max_days)

    print(json.dumps({"timestamp": timestamp, "connected": is_ok, "reason": reason}))
    os._exit(1 if not is_ok else 0)


if __name__ == "__main__":
    main()
