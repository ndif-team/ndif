---
title: Dashboard Internals
one_liner: The dashboard's FastAPI app and routers, file-backed schedule/cache stores, and the monitor and reconcile cron jobs.
tags: [internals, dev, dashboard]
related: [docs/operating/dashboard.md, docs/developing/dashboard-frontend.md, docs/developing/cli-internals.md, docs/developing/controller-internals.md, docs/developing/repo-layout.md, docs/reference/http-api.md, docs/operating/models-and-deployment.md, docs/concepts/sandbox-execution.md]
sources: [src/ndif/services/dashboard/backend/app.py, src/ndif/services/dashboard/backend/config.py, src/ndif/services/dashboard/backend/auth.py, src/ndif/services/dashboard/backend/schedule_store.py, src/ndif/services/dashboard/backend/cache_store.py, src/ndif/services/dashboard/backend/log_reader.py, src/ndif/services/dashboard/backend/ndif_client.py, src/ndif/services/dashboard/backend/routers, src/ndif/services/dashboard/jobs/monitor.py, src/ndif/services/dashboard/jobs/reconcile.py, pyproject.toml, docker/Dockerfile]
---

# Dashboard Internals

## What this covers

`src/ndif/services/dashboard/` — a FastAPI backend, a Vue SPA, and two cron
entrypoints. For running and using it, see `docs/operating/dashboard.md`; this
page is the code.

Two constraints shape it. **No database, no privileged API**: state is JSON files
under one data directory, and every mutating action calls into `src/ndif/cli/lib/`
— the same functions the `ndif` CLI uses — so this is a second front-end onto the
CLI, not a second control plane. **Several processes write the same files**: the
uvicorn worker(s), the reconcile cron and FastAPI background tasks all touch
`schedule.json`, `.reconcile.state.json` and `cache/values.json`, so every store
does `flock` + atomic-rename and reconcile is serialized end to end.

## The app

`create_app()` (`backend/app.py:30`) builds everything: CORS for the Vite dev
origins only (`http://localhost:5173` / `127.0.0.1:5173`, `allow_credentials=True`
so the session cookie survives the proxy), six routers, `/api/health`, and the SPA
fallback. Static serving is registered last, deliberately: `dist/assets` mounts at
`/assets`, then a catch-all `GET /{full_path:path}` (`app.py:63`) returns the file
if it exists and `index.html` otherwise, which is what makes `/schedule` survive a
hard refresh. FastAPI matches in registration order, so every `/api/*` route wins
over the catch-all. If `dist/index.html` is missing the catch-all isn't registered
and `GET /` returns a JSON hint (`app.py:70`). `app = create_app()` (`:88`) is what
`start.sh` gives uvicorn.

## Endpoints

Everything except `/api/health` and the auth routes depends on `require_auth` —
which is a no-op under `NDIF_DASHBOARD_DEV_MODE=true`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | none | `{"ok": true, "dev_mode": …}` |
| `POST` | `/api/auth/login` | none | Verify username + bcrypt hash, set the session cookie |
| `POST` | `/api/auth/logout` | none | Delete the cookie |
| `GET` | `/api/auth/me` | cookie | `{username, dev_mode}`; a 401 drives the SPA to `/login` |
| `GET` | `/api/monitor/connected` | yes | Concatenated `connected_*.log` entries |
| `GET` | `/api/monitor/models` | yes | Concatenated `models_*.log` entries |
| `GET` | `/api/monitor/cluster` | yes | Concatenated `cluster_*.log` entries |
| `GET` | `/api/schedule` | yes | List events |
| `POST` | `/api/schedule` | yes | Create (201); canonicalizes, then background-reconciles |
| `GET` | `/api/schedule/{id}` | yes | One event, 404 if absent |
| `PUT` | `/api/schedule/{id}` | yes | Update; canonicalizes, then background-reconciles |
| `DELETE` | `/api/schedule/{id}` | yes | Delete (204); background-reconciles |
| `GET` | `/api/status` | yes | Controller status via Ray, deduped, aggregated, pinned-tagged |
| `POST` | `/api/deployments/deploy` | yes | Single-model deploy; also bumps the autocomplete cache |
| `POST` | `/api/deployments/evict` | yes | Evict one `model_key`, optionally one `replica` |
| `POST` | `/api/deployments/restart` | yes | Restart one `model_key`, optionally one `replica` |
| `GET` | `/api/cache` | yes | The autocomplete MRU lists |

Two files, confusingly named: `routers/deploy.py` owns the bare `/api` prefix
(now just `/api/status`); `routers/deployments.py` owns `/api/deployments/*`. The
SPA calls the singular-model endpoints plus `/api/status`.

### `/api/status`

`status_endpoint` (`routers/deploy.py:57`) is the most transformed response in the
backend. It calls `ndif_client.status()` — a direct Ray RPC to the controller
actor, **not** the API service's `/status` — then applies three transforms:

1. **Dedupe HF-cache shadows.** The controller surfaces the local HF cache
   listing as COLD entries, so a repo fetched under two casings appears twice. A
   COLD entry is dropped when a non-COLD entry exists for the same
   case-insensitive `(repo_id, revision)`.
2. **Replica aggregation.** `_aggregate_by_model_key` (`:142`) collapses the
   controller's one-entry-per-replica payload into one card per `model_key` with a
   `replicas: [...]` array; card-level fields take the best value across siblings
   (`HOT > WARM > COLD`, `RUNNING > DEPLOYING > NOT_STARTED > UNHEALTHY`,
   `pinned`/`trusted` OR'd). COLD entries have no `model_key` and pass through
   keyed by app name with empty `replicas`.
3. **Pinned tag.** `pinned` is OR'd with "this `model_key` matches an active
   schedule event", using the exact key stamped on the event at write time.

Bypassing the API's cached `/status` is deliberate: the Deployments view reloads
right after every action, and the API's Redis cache (`NDIF_STATUS_TTL_S`)
would show the *previous* level for up to its TTL.

### Error translation

`_call` (`routers/deployments.py:62`) wraps every `ndif_client` call:
`NDIFConnectivityError` → 503, `ValueError` → 400, anything else → 500 with
`f"{type(e).__name__}: {e}"` — the broad catch is what gets an HF `OSError` on a
gated repo into the browser toast readably. `_log_action` (`:82`) logs the lib's
transcript and per-model failures, since the deploy lib returns 200 even when
individual models fail and the only other record is a toast the user dismisses.

### Every dashboard deploy is `trusted: True`

`ModelSpec.trusted` and `DeployRequest.trusted` default to `True`
(`routers/deploy.py:34`, `routers/deployments.py:38`), and `_spec_from_event`
hardcodes it for scheduled deploys (`jobs/reconcile.py:62`). It rides through
`DeploymentConfig.trusted` (`common/schema/controller.py:36`) into
`trust_remote_code=` for both the controller's size evaluator
(`controller/cluster/cluster.py:169`) and the actor's model load
(`controller/controller.py:280`) — which must agree or GPU accounting is wrong —
so a checkpoint with custom modeling code runs that code in the model actor.

A *different* flag shares the name: `BackendRequestModel.trusted`
(`common/schema/request.py:57`) decides whether a user's block runs in-process or
in a runner subprocess (`services/ray/sandbox/model.py:242`), and is set at
ingress from the API key's `trusted` tag (`services/api/auth.py:75`) or to `True`
when auth isn't configured (`:174`) — never from the dashboard's deploy flag.

## Config and auth

`Settings` (`backend/config.py:32`) is a `pydantic_settings.BaseSettings` with
`env_prefix="NDIF_DASHBOARD_"` (`config.py:33`), so every field reads from a
`NDIF_DASHBOARD_*` variable. Derived paths (`logs_dir`,
`schedule_path`, `reconcile_state_path`, `monitor_config_path`, `cache_path`) are
properties hung off `data_dir`, and `get_settings()` (`:86`) is `lru_cache`d and
creates the directories as a side effect — so importing config guarantees the tree
exists, and env changes after the first call don't take effect.

`backend/auth.py` has no user model. `hash_password` / `verify_password` (`:48`,
`:39`) are bcrypt with an explicit 72-byte truncation (`_to_bytes`, `:35`), since
bcrypt 4.x raises rather than silently truncating. `issue_session_token` (`:52`)
serializes `{"u": username}` with
`itsdangerous.URLSafeTimedSerializer(secret, salt="ndif-dashboard")` and
`parse_session_token` (`:57`) loads it with `max_age = session_ttl_days * 86400`,
returning `None` on `BadSignature` / `SignatureExpired`. Nothing is stored
server-side — the cookie *is* the session, so rotating the secret invalidates every
session at once. `require_auth` (`:70`) short-circuits to `settings.username` under
`dev_mode`, else reads the `ndif_dashboard_session` cookie and 401s if it's
missing, unsigned, expired, or names a different user. `_cli` (`:90`) implements
`python -m ndif.services.dashboard.backend.auth hash <password>`, the only
supported way to produce `NDIF_DASHBOARD_PASSWORD_HASH`.

## schedule_store

Two Pydantic models and a class over one JSON file. `ScheduleEventIn` (`:53`) is
the write payload — `title`, `checkpoint`, `start`, optional `end`, plus the
deployment knobs — with a validator rejecting `end <= start` (`:77`).
`ScheduleEvent` (`:88`) is the persisted form: adds `id`, `created_at`,
`updated_at`, `last_status`, `last_error`, and narrows `model_key` to required,
because every persisted event has been canonicalized.

Every method wraps its read-modify-write in `_locked` (`:108`), an
`fcntl.flock(LOCK_EX)` context manager. The lock lives on a **sidecar**
`schedule.json.lock` rather than the data file because `_write_json` (`:133`)
replaces the data file via `tempfile.mkstemp` + `os.replace`, and an flock on the
original inode would be orphaned by the rename. `list()` takes the lock too, and
`_read_json` (`:124`) returns `{"events": []}` for both a missing and a corrupt
file — the store never raises on a bad file. `mark_status` (`:205`) is reconcile's
write path. Activity is a pure function of event and timestamp: `_is_active`
(`:221`) is `start <= when and (end is None or when < end)`, and `filter_active`
(`:229`) is exported so reconcile and `/api/status` share the predicate.

## cache_store

Backs the autocomplete dropdowns: three MRU lists (`repo_id`, `actor_class`,
`envoy_class`) capped at `MAX_ENTRIES_PER_FIELD = 200` in
`<data_dir>/cache/values.json`, same sidecar-flock + atomic-rename discipline.
`add_many` (`:89`) bumps each recognized field to the front, ignoring unknown keys
and falsy values. `add_from_deploy_result` (`:112`) is the real entry point: it
pairs input specs to the deploy lib's results by `checkpoint`, **skips anything
carrying an `error`**, and writes the canonical `repo_id` parsed out of the
returned `model_key` rather than the string the user typed, so `…-8b` and `…-8B`
collapse to the form HF serves. Only successful deploys land in the cache.

## log_reader and the monitor JSONL format

`backend/log_reader.py` is 45 lines. `parse_log_files(log_dir, pattern)` globs,
sorts by filename (chronological — files are `*_YYYY-MM-DD.log`), and parses one
JSON object per line, skipping blanks and unparseable lines; `read_connected` /
`read_models` / `read_cluster` are the three patterns. No pagination, no time
filter, no cap — the router returns the whole 30-day window and the SPA slices
client-side. The shapes, from `jobs/monitor.py`:

```jsonc
// connected_*.log (monitor.py:260) — one per tick; status is "ok" or the reason
{"timestamp": "2026-07-22T18:00:00+00:00", "status": "ok"}
// models_*.log (monitor.py:266) — one per trace pass; "degraded" if any failed
{"timestamp": "...", "status": "ok", "ok": 3, "total": 4,
 "results": [{"model": "openai-community/gpt2", "status": "ok", "latency_s": 1.23},
             {"model": "…", "status": "error", "latency_s": 4.5, "error": "..."}]}
```

`cluster_*.log` (`monitor.py:253`) adds `nodes`, `total_gpus`, `total_memory_bytes`,
`available_memory_bytes` and a `node_details` array of `{node_id, gpus,
memory_bytes, available_bytes, deployments}`.

Per-model `status` is `ok`, `error`, `load_error` (the nnsight wrapper couldn't be
reconstructed) or `timeout`.

## ndif_client

55 lines of adaptation over `src/ndif/cli/lib/`. The lib functions stream
progress through an `on_message` callback (the CLI passes `click.echo`);
`_with_logs` (`:31`) injects a list-appending callback and stitches the lines into
`result["logs"]`, so an HTTP response or cron log carries the transcript the CLI
prints. `deploy`/`evict`/`restart` are wrapped, `status` is re-exported unwrapped,
`evict_all(**kw)` is `evict(evict_all=True, **kw)`, and `NDIFConnectivityError` is
re-exported. Ray connection and address resolution live in the lib — see
`docs/developing/cli-internals.md`.

## jobs/monitor.py

A one-shot script. `main()` (`:399`) arms a `SIGALRM` for
`SCRIPT_TIMEOUT = 480`s that `os._exit(2)`s a wedged run, then takes a
non-blocking flock on `logs/.monitor.lock` and exits 0 if held (`acquire_lock`,
`:381`) — a slow tick skips rather than overlapping. `probe_health` (`:129`) does
`GET /connected` then `GET /status`, calling the deployment down for an
unreachable API, a 503, an unreachable `/status`, **or zero HOT models**;
`/connected` alone is too shallow, since the Ray client can be alive while the
controller is wedged. A cluster snapshot is written whenever `/status` answered,
healthy or not. If healthy and `model_check_due` (`:222`) — first run, recovery
from down, or `--model-interval` (default 7200s) elapsed — it runs one remote
trace per HOT model. Finally it writes the connectivity datapoint, applies the
up/down transition (`record_status_transition`, `:350`), saves `.state.json`,
rotates logs, and `os._exit(1 if not is_ok else 0)`.

`_run_trace` (`:155`) needs nnsight installed in the dashboard environment: it
reconstructs the wrapper from the full `model_key` via `Remotable.from_model_key`
rather than hardcoding a class (so VLM and PEFT deployments are exercised under
their own envoy class), then runs
`with model.trace("Hello", remote=True): model.output.save()`. It sets
`CONFIG.API.HOST` from `--api-host or --url`; without that nnsight defaults to
`https://api.ndif.us` and the probe silently tests the public deployment. Each
trace runs in a one-worker thread pool so `--model-timeout` (default 60s) can
abandon a hung request (`check_model`, `:194`).

Discord notification is edge-triggered: `notify_if_failures_changed` (`:342`)
compares the sorted failed-model set to the one in `.state.json` and posts only on
a change, and `notify_status` (`:285`) posts on up→down, down→up and still-down.
`notify_model_failures` (`:304`) adds models one at a time and stops before
Discord's 2000-char limit — server-side tracebacks in `error` easily exceed the
whole budget alone.

## jobs/reconcile.py

The entry point is `reconcile_once(force=False)` (`:173`), used by cron
(`main()`, `:326`) and by the FastAPI background task. It is nothing but
`_reconcile_lock` around `_reconcile_locked` (`:190`).

`_reconcile_lock` (`:148`) is a **blocking** exclusive flock on
`.reconcile.state.json.lock` — blocking rather than skip-if-held because each
schedule write queues its own background reconcile while the 2-minute cron runs
alongside. Unserialized, each pass would read the same stale `prev_model_keys`,
diff against it, and the last writer would win, leaking deployments or dropping
evictions. The critical section spans the whole read-diff-act-write sequence
including the deploy/evict RPCs, so it can be held for tens of seconds.

The diff (`:195`–`:230`):

```python
new_keys = {e.model_key: e for e in filter_active(store.list())}
prev_keys = set(state.get("prev_model_keys") or [])
to_evict = sorted(prev_keys - set(new_keys))
hot_keys = _fetch_hot_model_keys()            # live, via Ray
to_deploy_events = [ev for mk, ev in new_keys.items()
                    if mk not in prev_keys or mk not in hot_keys]
```

`_fetch_hot_model_keys` (`:84`) reads the controller through
`ndif_client.status()` and returns `None` on any Ray-side error. `None` skips the
pass entirely unless `--force` (`:214`) — acting blind would make every scheduled
model look drifted-out and stack a second pinned replica on the one already
serving.

The rest is bookkeeping: evict first, then one `deploy(specs, sync=False)`
(additive — `sync` would evict everything not in the spec list, fighting ad-hoc
deploys). If deploy raises after a successful evict, state is persisted anyway so
the next tick doesn't re-evict the same models (`:281`). Per-event
`last_status`/`last_error` come from the deploy result matched by `checkpoint`,
failures go to Discord via the `schedule_failed` template, and successful specs are
mirrored into the autocomplete cache (`:316`). `_spec_from_event` (`:57`) holds the
schedule's policy: `pinned=True` and `trusted=True`, hardcoded.

`routers/schedule.py:55`'s `_trigger_reconcile` imports `reconcile_once` **lazily**
(the import pulls in Ray and nnsight, otherwise paid at uvicorn startup) and
swallows every exception with a logged traceback — a failed background reconcile
must not turn a successful schedule write into a 500, and the cron retries.

## Frontend

The Vue 3 SPA has its own page: `docs/developing/dashboard-frontend.md` — layout,
the `api.ts` wrapper and router auth guard, per-view data flow, the `:5173` Vite
dev proxy, and the build. One fact belongs here too, because it decides whether a
deployed backend serves a UI at all: **`frontend/dist/` is committed and shipped.**
`pyproject.toml` ships `frontend/dist/*` as package-data and the `frontend_dist`
setting points at it (overridable via `NDIF_DASHBOARD_FRONTEND_DIST`), so a clean
checkout + `just up` serves the UI at `GET /` with no host-side `npm` — the image
never runs `node`, and a wheel carries the assets. Rebuild only if you change the
frontend: `npm ci && npm run build` in `frontend/`, then `just build dashboard`.

## Dependencies

The `dashboard` extra is web-app only: `fastapi`, `uvicorn[standard]`,
`pydantic-settings`, `itsdangerous`, `bcrypt`, `python-multipart`, `requests`.
Both crons need more: reconcile drives `cli/lib`, which uses the Ray and redis
clients, so it needs the `ray` extra (`requirements.in` pins
`ray[client]==2.55.1` to match `services/ray`), and the monitor's probe needs
nnsight plus `peft` / `torchvision` for PEFT and VLM checkpoints. The container
sidesteps this by installing every extra into one image.

## Related

- `docs/operating/dashboard.md` — running it, auth setup, the schedule model.
- `docs/developing/dashboard-frontend.md` — the Vue SPA, its dev loop, and the
  build prerequisite.
- `docs/developing/cli-internals.md` — `cli/lib/{deploy,evict,restart,status}`,
  where every mutation actually happens.
- `docs/developing/controller-internals.md` — the other side of the Ray RPC.
- `docs/concepts/sandbox-execution.md` — what the request-level `trusted` flag
  turns off.
