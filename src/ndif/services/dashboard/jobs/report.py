#!/usr/bin/env python3
"""Periodic usage + uptime report cron.

Runs once a day (configurable) and posts a headline summary to Discord. Unlike
``monitor``, which is an alerting job that fires on transitions, this is a
digest: it answers "what happened on NDIF today?" in one message.

Two sources, deliberately:

1. **Uptime** comes from the connectivity datapoints ``monitor`` already writes
   to ``connected_*.log`` every tick. That is the dashboard's own record of
   whether the deployment answered, so the report needs no extra probing and
   cannot disagree with the alerts that were already sent.
2. **Usage** comes from InfluxDB, the durable metrics store — request counts,
   outcomes, active users, models, latency percentiles and bytes moved. Loki is
   not consulted: everything headline-worthy is already a metric, and log
   retention is usually shorter than the metric retention a daily report wants.

Fail-open like the rest of the telemetry path: if InfluxDB is unreachable the
usage block is reported as unavailable rather than the whole job dying, so an
outage still produces an uptime report (which is exactly when you want one).

Invoked from cron as::

    python -m ndif.services.dashboard.jobs.report
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
from typing import Any, Optional

import requests

from .util import (
    DEFAULT_CONFIG,
    DEFAULT_LOG_DIR,
    DEFAULT_MAX_DAYS,
    TIMEOUT,
    load_config,
    rotate_logs,
    send_discord,
)

DEFAULT_WINDOW_HOURS = 24
DEFAULT_TOP_N = 3
DEFAULT_API_URL = "http://localhost:8001"

# One message, so it has to stay inside Discord's 2000-char limit. The stats are
# bounded (top_n rows each), so only a pathological model_key can push it over;
# ``format_report`` truncates as a backstop.
DISCORD_LIMIT = 2000

DEFAULT_MESSAGE = (
    "📊 **NDIF report** — {window}\n"
    "**Uptime** {uptime}\n"
    "**Requests** {requests}\n"
    "**Users** {users}\n"
    "**Models** {models}\n"
    "**Latency** {latency}\n"
    "**Data** {data}"
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR))
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS)
    p.add_argument(
        "--window-hours",
        type=float,
        default=float(os.environ.get("NDIF_DASHBOARD_REPORT_WINDOW_HOURS", DEFAULT_WINDOW_HOURS)),
        help="How far back the report looks (default 24).",
    )
    p.add_argument("--influx-url", default=os.environ.get("NDIF_INFLUX_URL", "http://localhost:8086"))
    p.add_argument("--influx-token", default=os.environ.get("NDIF_INFLUX_TOKEN", ""))
    p.add_argument("--influx-org", default=os.environ.get("NDIF_INFLUX_ORG", "ndif"))
    p.add_argument("--influx-bucket", default=os.environ.get("NDIF_INFLUX_BUCKET", "metrics"))
    p.add_argument(
        "--environment",
        default=os.environ.get("NDIF_ENVIRONMENT", ""),
        help="Restrict usage stats to one environment tag (default: all).",
    )
    p.add_argument("--api-url", default=os.environ.get("NDIF_API_URL", DEFAULT_API_URL))
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report instead of posting it to Discord.",
    )
    return p.parse_args()


# --------------------------------------------------------------------- uptime


def summarize_uptime(log_dir: Path, since: datetime.datetime) -> dict:
    """Uptime over the window, from the monitor cron's connectivity datapoints.

    Each line is ``{"timestamp": iso, "status": "ok" | <reason>}``. An outage is
    a run of consecutive non-ok checks; its length is measured from the first bad
    timestamp to the next good one, so it does not assume the cron interval.
    """
    entries: list[dict] = []
    for path in sorted(log_dir.glob("connected_*.log")):
        try:
            text = path.read_text()
        except OSError:
            continue
        for raw in text.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
                stamped = datetime.datetime.fromisoformat(entry["timestamp"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if stamped >= since:
                entries.append({"at": stamped, "ok": entry.get("status") == "ok"})

    entries.sort(key=lambda e: e["at"])
    if not entries:
        return {"checks": 0}

    ok = sum(1 for e in entries if e["ok"])
    outages: list[float] = []
    start: Optional[datetime.datetime] = None
    for entry in entries:
        if not entry["ok"] and start is None:
            start = entry["at"]
        elif entry["ok"] and start is not None:
            outages.append((entry["at"] - start).total_seconds())
            start = None
    if start is not None:  # still down at the end of the window
        outages.append((entries[-1]["at"] - start).total_seconds())

    return {
        "checks": len(entries),
        "ok": ok,
        "pct": round(100.0 * ok / len(entries), 2),
        "outages": len(outages),
        "longest_outage_s": round(max(outages)) if outages else 0,
        "down_now": not entries[-1]["ok"],
    }


# ---------------------------------------------------------------------- usage


class Influx:
    """Minimal read-side client. The shared ``InfluxProvider`` is write-only."""

    def __init__(self, url: str, token: str, org: str, bucket: str, environment: str = ""):
        self.url, self.token, self.org, self.bucket = url, token, org, bucket
        self.environment = environment
        self._api = None

    def __enter__(self) -> "Influx":
        from influxdb_client import InfluxDBClient

        self._client = InfluxDBClient(url=self.url, token=self.token, org=self.org, timeout=TIMEOUT * 1000)
        self._api = self._client.query_api()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    def scope(self) -> str:
        """Optional environment filter, as a Flux predicate fragment."""
        return f' and r.environment == "{self.environment}"' if self.environment else ""

    def rows(self, flux: str) -> list[dict]:
        """Run Flux and flatten to ``[{**tags, "value": v}]``."""
        out: list[dict] = []
        for table in self._api.query(flux):
            for record in table.records:
                row = {k: v for k, v in record.values.items() if not k.startswith("_")}
                row["value"] = record.get_value()
                out.append(row)
        return out


def summarize_usage(influx: Influx, hours: float) -> dict:
    """Headline usage numbers for the window."""
    # Whole minutes: Flux durations are integer-valued, so a float --window-hours
    # would emit "-24.0h" and fail the parse.
    window = f"-{max(1, int(round(hours * 60)))}m"
    bucket, scope = influx.bucket, influx.scope()

    def counts_by(measurement: str, field: str, column: str) -> list[dict]:
        return influx.rows(
            f'from(bucket: "{bucket}")'
            f" |> range(start: {window})"
            f' |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}"{scope})'
            f' |> group(columns: ["{column}"]) |> count() |> group()'
        )

    def total(measurement: str, field: str, fn: str = "sum") -> float:
        rows = influx.rows(
            f'from(bucket: "{bucket}")'
            f" |> range(start: {window})"
            f' |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "{field}"{scope})'
            f" |> group() |> {fn}()"
        )
        return float(rows[0]["value"]) if rows else 0.0

    def quantile(q: float) -> Optional[float]:
        rows = influx.rows(
            f'from(bucket: "{bucket}")'
            f" |> range(start: {window})"
            f' |> filter(fn: (r) => r._measurement == "execution_time" and r._field == "exec_ms"'
            f' and r.status == "completed"{scope})'
            f" |> group() |> quantile(q: {q})"
        )
        return float(rows[0]["value"]) if rows else None

    by_status = {r.get("status", "?"): int(r["value"]) for r in counts_by("execution_time", "exec_ms", "status")}
    by_email = {r.get("email", "?"): int(r["value"]) for r in counts_by("request_size", "payload_bytes", "email")}
    by_model = {r.get("model_key", "?"): int(r["value"]) for r in counts_by("request_size", "payload_bytes", "model_key")}

    executed = sum(by_status.values())
    completed = by_status.get("completed", 0)
    received = sum(by_email.values())
    users = {k: v for k, v in sorted(by_email.items(), key=lambda kv: -kv[1]) if k and k != "?"}
    # Headline failures are counted against *received*, not against executed:
    # a request that died before it ever reached a replica never gets an
    # execution_time point, so an executed-based rate hides it entirely.
    failed = max(0, received - completed)
    return {
        "received": received,
        "executed": executed,
        "completed": completed,
        "errored": executed - completed,   # execution-stage failures only
        "failed": failed,                  # everything that did not complete
        "error_rate": round(100.0 * failed / received, 1) if received else 0.0,
        "by_status": by_status,
        # Requests with no email tag — every request when auth is off, and
        # otherwise the ones that failed before the key was resolved. Surfaced
        # so the per-user numbers visibly add up to the received count.
        "unattributed": max(0, received - sum(users.values())),
        "users": users,
        "models": {k: v for k, v in sorted(by_model.items(), key=lambda kv: -kv[1]) if k and k != "?"},
        "exec_p50_ms": quantile(0.5),
        "exec_p95_ms": quantile(0.95),
        "ingress_bytes": int(total("request_size", "payload_bytes")),
        "egress_bytes": int(total("response_size", "response_bytes")),
    }


def hot_models(api_url: str) -> Optional[int]:
    """How many deployments are HOT right now. Best-effort garnish."""
    try:
        resp = requests.get(f"{api_url}/status", timeout=TIMEOUT)
        if not resp.ok:
            return None
        deployments = resp.json().get("deployments", {})
        return sum(1 for d in deployments.values() if str(d.get("deployment_level", "")).upper() == "HOT")
    except Exception:
        return None


# ----------------------------------------------------------------- formatting


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def shorten_model(model_key: str) -> str:
    """``...TransformersModel:{"repo_id": "openai-community/gpt2", ...}`` -> the repo id."""
    marker = '"repo_id": "'
    if marker in model_key:
        rest = model_key.split(marker, 1)[1]
        return rest.split('"', 1)[0]
    return model_key.rsplit(":", 1)[-1][:60]


def _top(counts: dict, n: int, key: Any = str) -> str:
    items = list(counts.items())[:n]
    return ", ".join(f"{key(k)} ({v})" for k, v in items) if items else "none"


def format_report(report: dict, config: dict) -> str:
    """Render the report with the configured template."""
    uptime, usage = report["uptime"], report.get("usage")
    top_n = int(config.get("report", {}).get("top_n", DEFAULT_TOP_N))

    if uptime.get("checks"):
        bits = [f"{uptime['pct']}% ({uptime['ok']}/{uptime['checks']} checks)"]
        if uptime["outages"]:
            bits.append(
                f"{uptime['outages']} outage(s), longest {human_duration(uptime['longest_outage_s'])}"
            )
        if uptime.get("down_now"):
            bits.append("**currently DOWN**")
        uptime_line = " · ".join(bits)
    else:
        uptime_line = "no connectivity data (is the monitor cron running?)"

    if usage is None:
        blank = "telemetry unavailable"
        requests_line = users_line = models_line = latency_line = data_line = blank
    elif not usage["received"] and not usage["executed"]:
        blank = "no activity"
        requests_line = users_line = models_line = latency_line = data_line = blank
    else:
        requests_line = (
            f"{usage['received']} received · {usage['completed']} completed · "
            f"{usage['failed']} failed ({usage['error_rate']}%)"
        )
        users_line = f"{len(usage['users'])} active · {_top(usage['users'], top_n)}"
        if usage["unattributed"]:
            users_line += f" · {usage['unattributed']} unattributed"
        models_line = f"{len(usage['models'])} used · {_top(usage['models'], top_n, shorten_model)}"
        p50, p95 = usage["exec_p50_ms"], usage["exec_p95_ms"]
        latency_line = (
            f"p50 {p50:.0f} ms · p95 {p95:.0f} ms" if p50 is not None and p95 is not None else "no completed requests"
        )
        data_line = f"{human_bytes(usage['ingress_bytes'])} in · {human_bytes(usage['egress_bytes'])} out"

    if report.get("hot_models") is not None:
        models_line += f" · {report['hot_models']} HOT now"

    template = config.get("messages", {}).get("daily_report", DEFAULT_MESSAGE)
    message = template.format(
        window=report["window"],
        uptime=uptime_line,
        requests=requests_line,
        users=users_line,
        models=models_line,
        latency=latency_line,
        data=data_line,
    )
    if len(message) > DISCORD_LIMIT:
        message = message[: DISCORD_LIMIT - 3] + "..."
    return message


# ----------------------------------------------------------------------- main


def build_report(args: argparse.Namespace, log_dir: Path) -> dict:
    """Assemble the report. Importable so an endpoint could serve the same data."""
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(hours=args.window_hours)

    report: dict = {
        "generated_at": now.isoformat(),
        "window_hours": args.window_hours,
        "window": f"{since.strftime('%Y-%m-%d %H:%M')} → {now.strftime('%H:%M')} UTC",
        "uptime": summarize_uptime(log_dir, since),
        "usage": None,
    }

    try:
        with Influx(
            args.influx_url, args.influx_token, args.influx_org, args.influx_bucket, args.environment
        ) as influx:
            report["usage"] = summarize_usage(influx, args.window_hours)
    except Exception as e:  # fail open: an uptime-only report still beats none
        report["usage_error"] = f"{type(e).__name__}: {e}"
        print(f"InfluxDB query failed, reporting uptime only: {e}")

    report["hot_models"] = hot_models(args.api_url)
    return report


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config))
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    report = build_report(args, log_dir)
    message = format_report(report, config)

    with open(log_dir / f"report_{datetime.date.today().isoformat()}.log", "a") as f:
        f.write(json.dumps(report) + "\n")
    rotate_logs(log_dir, "report_*.log", args.max_days)

    if args.dry_run:
        print(message)
        return

    webhook_url = config.get("discord_webhook")
    if not webhook_url:
        print("No discord_webhook configured; report written to the log only.")
        print(message)
        return
    send_discord(webhook_url, message)


if __name__ == "__main__":
    main()
