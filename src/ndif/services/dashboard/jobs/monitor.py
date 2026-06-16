#!/usr/bin/env python3
"""Connectivity + model-trace monitor cron.

Runs every few minutes from cron. Each tick:

1. Probes ``/connected`` (Ray client is alive) and ``/status`` (Controller
   actor is responsive + has at least one HOT model). ``/connected`` alone
   is too shallow — the Ray client can be alive while the Controller is
   wedged — so we always need both.
2. Logs cluster snapshot to ``cluster_*.log`` whenever ``/status`` returns.
3. Every ``--model-interval`` seconds (default 2h), or on recovery from
   a down state, also runs a remote nnsight trace against each HOT model
   and logs the result to ``models_*.log``.
4. Writes one connectivity datapoint to ``connected_*.log`` and updates
   ``.state.json`` with the down/up transition. Fires Discord notifications
   on transitions and on newly-failed model sets.

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


DEFAULT_URL = "http://localhost:5001"
DEFAULT_MODEL_TIMEOUT = 60
DEFAULT_MODEL_INTERVAL = 7200  # 2 hours
SCRIPT_TIMEOUT = 480

DEFAULT_MESSAGES = {
    "down": "🔴 **NDIF is DOWN** — {reason} at {timestamp} {mention}",
    "still_down": "🔴 **NDIF is still down** — {reason} at {timestamp} (down since {down_since})",
    "back_up": "🟢 **NDIF is back UP** — Recovered at {timestamp} (was down since {down_since}) {mention}",
    "models_failed": "⚠️ **{failed_count}/{total} model(s) failed** {mention}\n{model_list}",
}


# ---- Connectivity probes ----

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


def probe_health(base_url: str) -> tuple[bool, str, dict | None]:
    """Return ``(is_ok, reason, status_data)``.

    ``status_data`` is returned even when ``is_ok`` is False (e.g. zero HOT
    models) so the caller can still write the cluster snapshot. It's ``None``
    only when ``/status`` itself failed or wasn't attempted.
    """
    try:
        ok, reason = check_connected(base_url)
    except requests.RequestException:
        return False, "API unreachable", None
    if not ok:
        return False, reason, None

    try:
        status_data = get_status(base_url)
    except Exception:
        return False, "/status unreachable", None

    if not extract_hot_models(status_data):
        return False, "no HOT models deployed", status_data
    return True, "ok", status_data


# ---- Model traces ----

def _run_trace(model_key: str, api_key: str, api_host: str | None = None) -> dict:
    """Trace a remote ``"Hello"`` against the given model.

    Reconstructs the nnsight wrapper from the full ``model_key`` via
    ``RemoteableMixin.from_model_key``, which handles class + repo_id +
    revision in one shot. This avoids hardcoding ``LanguageModel`` so VLMs
    and other envoy types are exercised under their own wrapper class.
    """
    from nnsight import CONFIG
    from nnsight.modeling.mixins import RemoteableMixin

    CONFIG.API.APIKEY = api_key
    # Point traces at this deployment's API; otherwise nnsight defaults to
    # https://api.ndif.us (prod) and the probe silently checks the wrong stack.
    if api_host:
        CONFIG.API.HOST = api_host
    result = {"model": model_key}

    try:
        model = RemoteableMixin.from_model_key(model_key, trust_remote_code=True)
    except Exception as e:
        result["status"] = "load_error"
        result["error"] = str(e)
        return result

    start = time.perf_counter()
    try:
        with model.trace("Hello", remote=True):
            model.output.save()
        result["status"] = "ok"
        result["latency_s"] = round(time.perf_counter() - start, 3)
    except Exception as e:
        result["status"] = "error"
        result["latency_s"] = round(time.perf_counter() - start, 3)
        result["error"] = str(e)

    return result


def check_model(model_key: str, api_key: str, model_timeout: int, api_host: str | None = None) -> dict:
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run_trace, model_key, api_key, api_host)
    try:
        return future.result(timeout=model_timeout)
    except concurrent.futures.TimeoutError:
        return {"model": model_key, "status": "timeout", "error": f"Exceeded {model_timeout}s timeout"}
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def run_model_traces(hot_models: list[dict], api_key: str, timeout_s: int, api_host: str | None = None) -> list[dict]:
    results = []
    for m in hot_models:
        repo_id = m.get("repo_id", "unknown")
        model_key = m.get("model_key")
        if not model_key:
            print(f"Skipping {repo_id}: no model_key in /status", flush=True)
            continue
        print(f"Checking {repo_id}...", flush=True)
        result = check_model(model_key, api_key, timeout_s, api_host)
        # Surface the human-readable repo_id in logs/notifications.
        result["model"] = repo_id
        results.append(result)
        print(f"  {result['status']} ({result.get('latency_s', 'N/A')}s)")
    return results


def model_check_due(state: dict, now_ts: str, interval_s: int) -> bool:
    """True if the trace pass should run this tick.

    Runs on recovery from down (``last_status != "ok"``), on first-ever run
    (``last_model_check is None``), or once ``interval_s`` has elapsed.
    """
    if state["last_status"] != "ok":
        return True
    last_mc = state.get("last_model_check")
    if last_mc is None:
        return True
    elapsed = (datetime.datetime.fromisoformat(now_ts) -
               datetime.datetime.fromisoformat(last_mc)).total_seconds()
    return elapsed >= interval_s


# ---- State / log files ----

def load_state(log_dir: Path) -> dict:
    state_file = log_dir / ".state.json"
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {"last_status": "ok", "down_since": None, "last_model_check": None}


def save_state(log_dir: Path, state: dict) -> None:
    with open(log_dir / ".state.json", "w") as f:
        json.dump(state, f)


def write_cluster_log(log_dir: Path, today: str, timestamp: str, status_data: dict) -> None:
    info = extract_cluster_info(status_data)
    info["timestamp"] = timestamp
    with open(log_dir / f"cluster_{today}.log", "a") as f:
        f.write(json.dumps(info) + "\n")


def write_connected_log(log_dir: Path, today: str, timestamp: str, is_ok: bool, reason: str) -> None:
    entry = {"timestamp": timestamp, "status": "ok" if is_ok else reason}
    with open(log_dir / f"connected_{today}.log", "a") as f:
        f.write(json.dumps(entry) + "\n")


def write_models_log(log_dir: Path, today: str, timestamp: str, results: list[dict]) -> None:
    ok_count = sum(1 for r in results if r["status"] == "ok")
    entry = {
        "timestamp": timestamp,
        "status": "ok" if ok_count == len(results) else "degraded",
        "ok": ok_count, "total": len(results),
        "results": results,
    }
    with open(log_dir / f"models_{today}.log", "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---- Notifications ----

def format_discord_ts(iso_timestamp: str) -> str:
    dt = datetime.datetime.fromisoformat(iso_timestamp)
    return f"<t:{int(dt.timestamp())}:f>"


def notify_status(config: dict, was_ok: bool, down_since: str | None, is_ok: bool, reason: str, timestamp: str) -> None:
    webhook_url = config.get("discord_webhook")
    if not webhook_url:
        return
    messages = {**DEFAULT_MESSAGES, **config.get("messages", {})}
    fmt = {
        "reason": reason,
        "timestamp": format_discord_ts(timestamp),
        "down_since": format_discord_ts(down_since) if down_since else "unknown",
        "mention": get_mention(config),
    }
    if was_ok and not is_ok:
        send_discord(webhook_url, messages["down"].format(**fmt))
    elif not was_ok and not is_ok:
        send_discord(webhook_url, messages["still_down"].format(**fmt))
    elif not was_ok and is_ok:
        send_discord(webhook_url, messages["back_up"].format(**fmt))


def notify_model_failures(config: dict, failed: list, total: int) -> None:
    """Discord-notify the set of failed models, capped at Discord's 2000-char limit.

    Per-model error strings can be huge (full Python tracebacks from the
    server side) — cap each line and the whole message so the webhook
    always succeeds. Verbose tracebacks remain in models_*.log.
    """
    webhook_url = config.get("discord_webhook")
    if not webhook_url or not failed:
        return
    messages = {**DEFAULT_MESSAGES, **config.get("messages", {})}
    mention = get_mention(config)

    DISCORD_LIMIT = 2000
    PER_LINE_ERR_CHARS = 120

    def _summarize(err) -> str:
        s = str(err or "unknown").splitlines()[0].strip()
        return (s[:PER_LINE_ERR_CHARS] + "…") if len(s) > PER_LINE_ERR_CHARS else s

    def _build(included, dropped):
        lines = [f"> **{r['model']}** — `{r['status']}`: {_summarize(r.get('error'))}" for r in included]
        if dropped:
            lines.append(f"> …and {dropped} more (see models_*.log)")
        return messages["models_failed"].format(
            failed_count=len(failed), total=total,
            mention=mention, model_list="\n".join(lines),
        )

    # Add models one at a time; stop when the next would exceed the limit.
    included: list = []
    for r in failed:
        if len(_build(included + [r], len(failed) - (len(included) + 1))) > DISCORD_LIMIT:
            break
        included.append(r)
    send_discord(webhook_url, _build(included, len(failed) - len(included)))


def notify_if_failures_changed(config: dict, state: dict, results: list[dict]) -> None:
    failed = [r for r in results if r["status"] != "ok"]
    failed_set = sorted(r["model"] for r in failed)
    if failed and failed_set != state.get("last_failed_models", []):
        notify_model_failures(config, failed, len(results))
    state["last_failed_models"] = failed_set


def record_status_transition(config: dict, state: dict, is_ok: bool, reason: str, timestamp: str) -> None:
    """Apply the up/down state-machine transition and fire any Discord notify."""
    was_ok = state["last_status"] == "ok"
    down_since = timestamp if (not is_ok and was_ok) else state.get("down_since")
    if not is_ok and was_ok:
        state["down_since"] = timestamp
    elif is_ok:
        state["down_since"] = None
    state["last_status"] = "ok" if is_ok else "down"
    notify_status(config, was_ok, down_since, is_ok, reason, timestamp)


# ---- Setup ----

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    p.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--api-key", default=None)
    p.add_argument("--api-host", default=None)
    p.add_argument("--model-timeout", type=int, default=DEFAULT_MODEL_TIMEOUT)
    p.add_argument("--model-interval", type=int, default=DEFAULT_MODEL_INTERVAL)
    return p.parse_args()


def resolve_api_key(args: argparse.Namespace, config: dict) -> str | None:
    return args.api_key or os.environ.get("NDIF_API_KEY") or config.get("ndif_api_key")


def acquire_lock(log_dir: Path):
    """Single-instance lock. Returns the file handle — caller must keep it alive."""
    lock_file = open(log_dir / ".monitor.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another monitor instance is running, skipping")
        sys.exit(0)
    return lock_file


def _timeout_handler(signum, frame):
    print("Monitor script timed out, force exiting")
    os._exit(2)


# ---- Main ----

def main():
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(SCRIPT_TIMEOUT)

    args = parse_args()
    config = load_config(Path(args.config))
    api_key = resolve_api_key(args, config)
    # --url (NDIF_DASHBOARD_MONITOR_URL) is the single source of truth: the ping
    # and the model traces hit the same deployment. Without this, nnsight
    # defaults to https://api.ndif.us and the traces silently probe prod.
    # --api-host overrides only if you deliberately want to split the two.
    api_host = args.api_host or args.url

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    _lock = acquire_lock(log_dir)  # noqa: F841 — keep handle alive

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    today = datetime.date.today().isoformat()
    state = load_state(log_dir)

    is_ok, reason, status_data = probe_health(args.url)
    if status_data is not None:
        write_cluster_log(log_dir, today, timestamp, status_data)

    if is_ok and model_check_due(state, timestamp, args.model_interval):
        state["last_model_check"] = timestamp
        if api_key:
            results = run_model_traces(
                extract_hot_models(status_data), api_key, args.model_timeout, api_host,
            )
            write_models_log(log_dir, today, timestamp, results)
            notify_if_failures_changed(config, state, results)
        else:
            print("Warning: no API key set, skipping model traces")

    write_connected_log(log_dir, today, timestamp, is_ok, reason)
    record_status_transition(config, state, is_ok, reason, timestamp)
    save_state(log_dir, state)

    for pattern in ("connected_*.log", "models_*.log", "cluster_*.log"):
        rotate_logs(log_dir, pattern, args.max_days)

    print(json.dumps({"timestamp": timestamp, "connected": is_ok, "reason": reason}))
    os._exit(1 if not is_ok else 0)


if __name__ == "__main__":
    main()
