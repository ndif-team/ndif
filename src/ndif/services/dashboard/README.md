# NDIF Dashboard

A small admin web app for NDIF: monitoring (uptime, latency, cluster state)
and a calendar-driven scheduler for pinned deployments.

Three pieces:

| Piece | What it is |
|---|---|
| **`backend/`** | FastAPI app — auth, schedule CRUD, monitor log read endpoints, ad-hoc deploy/evict |
| **`jobs/`** | Cron entrypoints — `monitor.py` (uptime + model traces) and `reconcile.py` (push schedule → controller) |
| **`frontend/`** | Vue 3 + Vite + TS SPA — login, monitor view, deployments view, month calendar |

## Running it

The dashboard ships as a docker-compose service alongside `api` and `ray`.
The image is built from the unified `docker/Dockerfile` with `NAME=dashboard`;
the Vue SPA is pre-built on the host by `make dashboard-frontend` (host-side
`npm ci && npm run build`) and copied into the image. A `cron` daemon runs
alongside uvicorn for the monitor + reconcile jobs.

```bash
# 1. Generate a password hash + session secret (write to .env, see below)
python -m ndif.services.dashboard.backend.auth hash 'mypassword'
python -c 'import secrets; print(secrets.token_urlsafe(48))'

# 2. Set required env in <repo-root>/.env (gitignored):
#    NDIF_DASHBOARD_USERNAME=admin
#    NDIF_DASHBOARD_PASSWORD_HASH=$2b$12$...    (literal — no escaping)
#    NDIF_DASHBOARD_SESSION_SECRET=...
#    NDIF_API_KEY=...                                  # for monitor cron's model traces

# 3. Build images and bring the stack up:
make build && make up

# 4. Browse to http://<host>:8081/  (default port; override via NDIF_DASHBOARD_PORT)
```

For non-Docker dev (running uvicorn directly on the host), `start.sh` is
the same script the container uses. Set the env vars in your shell, build
the frontend (`cd frontend && npm install && npm run build`), then run:

```bash
bash src/ndif/services/dashboard/start.sh
```

The `cron` block inside `start.sh` only fires when `cron` is on PATH and
`/etc/cron.d` is writable, so on a typical dev laptop you'll just get
uvicorn — no host-side crontab is touched.

## Configuration

Env-var driven. Defaults come from `backend/config.py`. Everything below has
the prefix `NDIF_DASHBOARD_` unless noted.

| Env var | Default | Purpose |
|---|---|---|
| `NDIF_DASHBOARD_USERNAME` | `admin` | Admin username (single user) |
| `NDIF_DASHBOARD_PASSWORD_HASH` | (empty — login disabled) | Bcrypt hash; generate via the helper above |
| `NDIF_DASHBOARD_SESSION_SECRET` | unsafe placeholder | Sign session cookies; rotate to invalidate sessions |
| `NDIF_DASHBOARD_SESSION_TTL_DAYS` | `7` | Cookie lifetime |
| `NDIF_DASHBOARD_DEV_MODE` | `false` | If `true`, all routes bypass auth (frontend dev only) |
| `NDIF_DASHBOARD_DATA_DIR` | `~/ndif_dashboard` | Logs, `schedule.json`, `.reconcile.state.json`, `config.json` |
| `NDIF_DASHBOARD_FRONTEND_DIST` | `…/dashboard/frontend/dist` | Built Vue SPA to serve |
| `NDIF_DASHBOARD_PORT` | `8081` | Backend port |
| `NDIF_DASHBOARD_MONITOR_URL` | `http://localhost:5001` | Where the monitor cron probes. Override to the public URL (e.g. `https://api.ndif.us`) in prod so the probe exercises DNS + TLS + LB |
| `NDIF_DASHBOARD_MONITOR_CRON` | `*/10 * * * *` | Schedule for the uptime cron |
| `NDIF_DASHBOARD_RECONCILE_CRON` | `*/2 * * * *` | Schedule for the reconcile cron |
| `NDIF_API_URL` | from compose | Used by `/api/status` proxy. Reused from the rest of the stack. |
| `NDIF_API_KEY` | from `.env` | Required by the monitor cron's nnsight model traces |
| `NDIF_RAY_ADDRESS`, `NDIF_BROKER_URL` | from `.env.example` | Inherited by the reconcile cron's deploy/evict calls |

## Crons (inside the container)

`start.sh` writes `/etc/cron.d/ndif-dashboard` and starts the cron daemon:

```
*/10 * * * *  python -m ndif.services.dashboard.jobs.monitor    # uptime + traces
*/2  * * * *  python -m ndif.services.dashboard.jobs.reconcile  # push schedule → controller
```

The monitor cron writes JSONL to `<data_dir>/logs/connected_*.log`,
`models_*.log`, `cluster_*.log` and rotates on a 30-day window. The
dashboard's monitor view reads these files directly.

The FastAPI schedule routes also call `reconcile_once()` as a background task
on every write so user edits don't wait for the next 2-min tick. A file
lock around `reconcile_once` serializes concurrent runs (BG tasks + cron)
so rapid edits can't race.

## Schedule semantics

- One model per event, always `pinned=True`. `pinned` tells the controller
  "do not evict this" — it does NOT imply any sync behavior (the controller
  no longer has a sync mode).
- An event is "active" while `start ≤ now < end`. An event with `end is None`
  is open-ended (active forever after `start`).
- Per-event fields mirror `DeploymentConfig`: `revision`, `actor_class`,
  `padding_factor`, `execution_timeout_seconds`. `model_key` is server-set
  via HF resolution at write time.
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

Set `NDIF_DASHBOARD_DEV_MODE=true` on the backend if you want to skip the
login flow during development.

## Files

```
dashboard/
├── README.md
├── start.sh                # canonical runner — used by both Docker and standalone
├── config.example.json     # discord webhook + message templates
├── requirements.in         # backend pip deps
├── backend/
│   ├── app.py              # FastAPI app
│   ├── config.py           # env-driven settings
│   ├── auth.py             # bcrypt + signed cookie + CLI hash helper
│   ├── log_reader.py       # reads jobs/monitor.py output
│   ├── ndif_client.py      # wrappers around cli/lib (deploy / evict / restart / status)
│   ├── schedule_store.py   # JSON-backed CRUD with fcntl lock
│   └── routers/{auth,monitor,schedule,deployments,deploy}.py
├── jobs/
│   ├── util.py             # discord / log helpers (data_dir-aware)
│   ├── monitor.py          # uptime + model traces
│   └── reconcile.py        # push schedule.json → controller (diff-based, flock-serialized)
└── frontend/               # Vue 3 + Vite + TS
    ├── package.json
    ├── vite.config.ts
    ├── tsconfig.json
    ├── index.html
    └── src/{main.ts,App.vue,router.ts,api.ts,stores,views,components,styles}
```
