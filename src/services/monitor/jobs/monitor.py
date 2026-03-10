#!/usr/bin/env python3
"""Unified NDIF monitor — checks connectivity every run, checks models every --model-interval."""

import argparse
import concurrent.futures
import datetime
import json
import os
import sys
import time
from pathlib import Path

import requests

from util import (
    DEFAULT_CONFIG, DEFAULT_LOG_DIR, DEFAULT_MAX_DAYS, TIMEOUT,
    load_config, send_discord, get_mention, rotate_logs,
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
    elif resp.status_code == 503:
        return False, "service disconnected (503)"
    else:
        return False, f"unexpected status {resp.status_code}"


def get_hot_models(base_url: str) -> list[dict]:
    resp = requests.get(f"{base_url}/status", timeout=TIMEOUT)
    resp.raise_for_status()
    deployments = resp.json().get("deployments", {})
    return [m for m in deployments.values() if m.get("deployment_level") == "HOT"]


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
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run_trace, repo_id, api_key)
        try:
            return future.result(timeout=model_timeout)
        except concurrent.futures.TimeoutError:
            return {"model": repo_id, "status": "timeout", "error": f"Exceeded {model_timeout}s timeout"}


# ---- State ----

def load_state(log_dir: Path) -> dict:
    state_file = log_dir / ".state.json"
    if state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {"last_status": "ok", "down_since": None, "last_model_check": None}


def save_state(log_dir: Path, state: dict):
    with open(log_dir / ".state.json", "w") as f:
        json.dump(state, f)


# ---- Notifications ----

def notify_status(config: dict, state: dict, is_ok: bool, reason: str, timestamp: str):
    webhook_url = config.get("discord_webhook")
    if not webhook_url:
        return

    messages = {**DEFAULT_MESSAGES, **config.get("messages", {})}
    mention = get_mention(config)
    was_ok = state["last_status"] == "ok"
    fmt = {
        "reason": reason,
        "timestamp": timestamp,
        "down_since": state.get("down_since", "unknown"),
        "mention": mention,
    }

    if was_ok and not is_ok:
        send_discord(webhook_url, messages["down"].format(**fmt))
    elif not was_ok and not is_ok:
        send_discord(webhook_url, messages["still_down"].format(**fmt))
    elif not was_ok and is_ok:
        send_discord(webhook_url, messages["back_up"].format(**fmt))


def notify_model_failures(config: dict, failed: list, total: int):
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

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL, help="NDIF base URL")
    parser.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    parser.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--api-key", default=None, help="NDIF API key (or set NDIF_API_KEY env var)")
    parser.add_argument("--model-timeout", type=int, default=DEFAULT_MODEL_TIMEOUT, help="Per-model timeout in seconds")
    parser.add_argument("--model-interval", type=int, default=DEFAULT_MODEL_INTERVAL, help="Seconds between model checks (default: 7200)")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("NDIF_API_KEY")
    config = load_config(Path(args.config))
    if not api_key:
        api_key = config.get("ndif_api_key")

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    today = datetime.date.today().isoformat()
    state = load_state(log_dir)

    is_ok = True
    reason = "ok"

    # ---- Step 1: /connected ----
    connected_entry = {"timestamp": timestamp}
    try:
        is_ok, reason = check_connected(args.url)
        connected_entry["status"] = "ok" if is_ok else reason
    except requests.RequestException as e:
        is_ok = False
        reason = f"API unreachable"
        connected_entry["status"] = "unreachable"
        connected_entry["error"] = str(e)

    with open(log_dir / f"connected_{today}.log", "a") as f:
        f.write(json.dumps(connected_entry) + "\n")

    # ---- Step 2: /status + model traces (if connected and interval elapsed) ----
    model_check_due = False
    if is_ok:
        # If currently down, force full check every run until recovery
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
        # Fetch /status
        try:
            hot_models = get_hot_models(args.url)
        except Exception as e:
            is_ok = False
            reason = f"/status unreachable"
            hot_models = []

        # Verify at least one HOT model
        if is_ok and len(hot_models) == 0:
            is_ok = False
            reason = "no HOT models deployed"

        # Run model traces
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
                if failed:
                    notify_model_failures(config, failed, total)

    # ---- Step 3: Up/down notifications ----
    notify_status(config, state, is_ok, reason, timestamp)

    # ---- Step 4: Update state ----
    if not is_ok and state["last_status"] == "ok":
        state["down_since"] = timestamp
    elif is_ok:
        state["down_since"] = None
    state["last_status"] = "ok" if is_ok else "down"
    save_state(log_dir, state)

    # ---- Step 5: Rotate ----
    rotate_logs(log_dir, "connected_*.log", args.max_days)
    rotate_logs(log_dir, "models_*.log", args.max_days)

    print(json.dumps({"timestamp": timestamp, "connected": is_ok, "reason": reason}))
    if not is_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
