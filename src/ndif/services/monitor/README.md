# NDIF Monitor

Monitors NDIF API connectivity and model health, sends Discord notifications on status changes, and serves a dashboard for visualizing uptime and latency.

## How it works

A single script (`jobs/monitor.py`) runs every 10 minutes via cron:

1. Checks `/connected` — is the API reachable and returning 200?
2. Every 2 hours (or every run while recovering from downtime), fetches `/status` and runs an nnsight trace on each HOT model.
3. Any failure at any stage = NDIF is **down**. Only a full clean run (connected + status + all model traces) brings it back **up**.
4. Sends Discord notifications on state transitions: down, still down, back up. Model trace failures get a separate warning.

## Setup

```bash
# Set required env vars
export NDIF_API_KEY=your_key
export INSTALL_DIR=~/ndif_monitor   # optional, defaults to ~/ndif_monitor

# Run setup — creates conda env, copies source, installs cron
./run.sh
```

`run.sh` will:
- Create a `monitor` conda env (Python 3.12) if it doesn't exist
- Install the `ndif` package (editable) into that env, which pulls in the monitor's deps
- Create `<INSTALL_DIR>/config.json` from `config.example.json` if missing
- Install/update the cron job (invokes `python -m ndif.services.monitor.jobs.monitor`)

Re-run `run.sh` after making changes to deploy updates. Because the install is editable, code changes in the repo take effect immediately — `run.sh` only needs to re-run if you change dependencies or the cron schedule.

## Install directory structure

```
<INSTALL_DIR>/
  config.json         Discord webhook + message templates
  logs/
    .state.json       Up/down state between runs
    connected_*.log   Connectivity check results (one file per day)
    models_*.log      Model trace results (one file per day)
```

## Configuration

Edit `<INSTALL_DIR>/config.json`:

```json
{
    "discord_webhook": "https://discord.com/api/webhooks/...",
    "discord_role_id": "1252363632805806240",
    "messages": {
        "down": "...",
        "still_down": "...",
        "back_up": "...",
        "models_failed": "..."
    }
}
```

Message templates support these variables: `{reason}`, `{timestamp}`, `{down_since}`, `{mention}`, `{failed_count}`, `{total}`, `{model_list}`.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `INSTALL_DIR` | `~/ndif_monitor` | Where source, logs, and config live |
| `NDIF_API_KEY` | — | API key for nnsight remote traces |
| `MONITOR_CRON` | `*/10 * * * *` | Cron schedule expression |

## Dashboard

```bash
# Start the dashboard server from the monitor conda env
conda run -n monitor python -m ndif.services.monitor.dashboard.dashboard \
  --log-dir <INSTALL_DIR>/logs
```

Or use the full path printed by `run.sh`. The dashboard runs on port 8080 by default (`--port` to change, `--host` to bind to a specific address).

Features:
- Connectivity calendar — click a day to see a 24-hour timeline of 10-minute check slots
- Average and per-model latency charts
- Per-model uptime timelines (30 days, 2-hour resolution)
- Dark/light theme toggle
- Auto-refreshes every 5 minutes

## Source files (repo)

```
src/ndif/services/monitor/
  run.sh                  Setup and deploy script
  config.example.json     Example config (copied on first deploy)
  requirements.in         Monitor-scoped pip deps (flask, requests, nnsight)
  jobs/
    monitor.py            Unified monitor script (python -m ndif.services.monitor.jobs.monitor)
    util.py               Shared utilities (config, discord, log rotation)
  dashboard/
    dashboard.py          Flask backend (python -m ndif.services.monitor.dashboard.dashboard)
    dashboard.html        Frontend (Chart.js, retro lo-fi style)
```
