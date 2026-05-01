# NDIF Dashboard

A small admin web app for NDIF: monitoring (uptime, latency, cluster state)
and a calendar-driven scheduler for pinned deployments. Replaces the
standalone `services/monitor/` dashboard.

Three pieces:

| Piece | What it is |
|---|---|
| **`backend/`** | FastAPI app — auth, schedule CRUD, monitor log read endpoints, ad-hoc deploy/evict |
| **`jobs/`** | Cron entrypoints — `monitor.py` (uptime + model traces) and `reconcile.py` (push schedule → controller) |
| **`frontend/`** | Vue 3 + Vite + TS SPA — login, monitor view, month calendar |

## Quick start (head node, outside Docker)

```bash
# 1. Generate a password hash for the admin user
python -m ndif.services.dashboard.backend.auth hash 'mypassword'

# 2. Set required env vars (best put in your shell profile or systemd unit)
export DASHBOARD_USERNAME=admin
export DASHBOARD_PASSWORD_HASH='$2b$12$...'
export DASHBOARD_SESSION_SECRET=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')
export NDIF_API_KEY=...           # for monitor cron's model traces
export NDIF_RAY_ADDRESS=ray://...
export NDIF_BROKER_URL=redis://...

# 3. Install conda env, build frontend, install crons
./run.sh

# 4. Start the FastAPI backend (use systemd/pm2/nohup in production)
~/miniconda3/envs/dashboard/bin/python -m uvicorn \
    ndif.services.dashboard.backend.app:app \
    --host 0.0.0.0 --port 8081
```

Then point a browser at `http://<host>:8081/` and log in.

## Configuration

Everything is env-var driven. Defaults come from `backend/config.py`.

| Env var | Default | Purpose |
|---|---|---|
| `DASHBOARD_USERNAME` | `admin` | Admin username (single user) |
| `DASHBOARD_PASSWORD_HASH` | (empty — login disabled) | Bcrypt hash; generate via the helper above |
| `DASHBOARD_SESSION_SECRET` | unsafe placeholder | Sign session cookies; rotate to invalidate sessions |
| `DASHBOARD_SESSION_TTL_DAYS` | `7` | Cookie lifetime |
| `DASHBOARD_DEV_MODE` | `false` | If `true`, all routes bypass auth (frontend dev only) |
| `DASHBOARD_DATA_DIR` | `~/ndif_dashboard` | Logs, `schedule.json`, `.reconcile.state.json`, `config.json` |
| `DASHBOARD_FRONTEND_DIST` | `…/dashboard/frontend/dist` | Built Vue SPA to serve |
| `NDIF_API_KEY`, `NDIF_RAY_ADDRESS`, `NDIF_BROKER_URL` | from `.env.example` | Inherited by jobs/backend |
| `MONITOR_CRON` | `*/10 * * * *` | Schedule for the uptime cron |
| `RECONCILE_CRON` | `1-59/10 * * * *` | Schedule for the reconcile cron (offset 1m) |

## Crons

`run.sh` installs two:

```
*/10 * * * *      python -m ndif.services.dashboard.jobs.monitor   # uptime + traces
1-59/10 * * * *   python -m ndif.services.dashboard.jobs.reconcile # push schedule → controller
```

The monitor cron writes JSONL to `<data_dir>/logs/connected_*.log`,
`models_*.log`, `cluster_*.log` and rotates on a 30-day window — same format
the previous `services/monitor` job produced. The dashboard's monitor view
reads these files directly.

The reconcile cron diff-hashes the active set of scheduled events and only
pushes when the set changes (or on the first run after a restart). Empty
active set ⇒ all previously-pinned models get evicted via `--sync`.

The FastAPI schedule routes also call `reconcile_once()` as a background task
on every write so user edits don't wait for the next 10-min tick.

## Schedule semantics

- One model per event, always `pinned=True`. `pinned` tells the controller
  "do not evict this" — it does NOT imply any sync behavior (the controller
  no longer has a sync mode).
- An event is "active" while `start ≤ now < end`. An event with `end is None`
  is open-ended (active forever after `start`).
- Per-event fields mirror `DeploymentConfig`: `revision`, `actor_class`,
  `padding_factor`, `execution_timeout_seconds`.
- The reconcile cron does the diff itself:
  - `to_evict = previously-active − new-active` → explicit `evict()` call
  - `to_deploy = new-active − currently-HOT` → explicit `deploy(pinned=True)` call
  Drift recovery (NDIF restart, manual evict) is folded into the deploy step.

## Frontend dev

```bash
cd frontend
npm install
npm run dev      # serves on :5173, proxies /api → :8081
```

Set `DASHBOARD_DEV_MODE=true` on the backend if you want to skip the login
flow during development.

## Production build

`run.sh` runs `npm install && npm run build` automatically when `npm` is on
the PATH. Without npm, build manually:

```bash
cd frontend && npm install && npm run build
```

The FastAPI app will pick up `frontend/dist/` automatically.

## Files

```
dashboard/
├── README.md
├── run.sh                  # installer + cron wiring
├── config.example.json     # discord webhook + message templates
├── requirements.in         # backend pip deps
├── backend/
│   ├── app.py              # FastAPI app
│   ├── config.py           # env-driven settings
│   ├── auth.py             # bcrypt + signed cookie + CLI hash helper
│   ├── log_reader.py       # reads jobs/monitor.py output
│   ├── ndif_client.py      # wrappers around cli/lib/deploy_lib
│   ├── schedule_store.py   # JSON-backed CRUD with fcntl lock
│   └── routers/{auth,monitor,schedule,deploy}.py
├── jobs/
│   ├── util.py             # discord/log helpers (data_dir-aware)
│   ├── monitor.py          # uptime + model traces (== old services/monitor)
│   └── reconcile.py        # push schedule.json → controller
└── frontend/               # Vue 3 + Vite + TS
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── index.html
    └── src/{main.ts,App.vue,router.ts,api.ts,stores,views,components,styles}
```

## Future: Docker compose integration

This dashboard is intentionally deployed standalone for now. To roll it into
`docker/docker-compose.yml` later, you'll need:
- a `dashboard` service running uvicorn,
- two cron sidecars (or move the cron into APScheduler inside the FastAPI
  process — the `jobs/*.main()` entrypoints are already importable),
- a bind-mount or named volume for `DASHBOARD_DATA_DIR` so cron + backend
  share the same log/state files.
