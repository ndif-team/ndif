---
title: Dashboard Internals
one_liner: The dashboard's FastAPI app and routers, file-backed schedule/cache stores, and the monitor, reconcile and report cron jobs.
tags: [internals, dev, dashboard]
related: [docs/operating/dashboard.md, docs/developing/dashboard-frontend.md, docs/developing/cli-internals.md, docs/developing/controller-internals.md, docs/developing/repo-layout.md, docs/reference/http-api.md, docs/operating/models-and-deployment.md, docs/concepts/sandbox-execution.md]
sources: [src/ndif/services/dashboard/backend/app.py, src/ndif/services/dashboard/backend/config.py, src/ndif/services/dashboard/backend/auth.py, src/ndif/services/dashboard/backend/schedule_store.py, src/ndif/services/dashboard/backend/cache_store.py, src/ndif/services/dashboard/backend/log_reader.py, src/ndif/services/dashboard/backend/ndif_client.py, src/ndif/services/dashboard/backend/routers, src/ndif/services/dashboard/jobs/monitor.py, src/ndif/services/dashboard/jobs/reconcile.py, src/ndif/services/dashboard/jobs/report.py, pyproject.toml, docker/Dockerfile]
---

# Dashboard Internals

## What this covers

`src/ndif/services/dashboard/` — a FastAPI backend, a Vue SPA
([Dashboard frontend](dashboard-frontend.md)), and three cron entrypoints. Running and
using it is [Dashboard](../operating/dashboard.md); this page is the code. Symbols
below are in `backend/` or `jobs/` under that directory.

Two constraints shape it. **No database, no privileged API**: state is JSON files
under one data directory, and every mutation calls into `src/ndif/cli/lib/` — the
functions the `ndif` CLI uses — so this is a second front-end onto the CLI, not a
second control plane. **Several processes write the same files**: uvicorn workers,
the reconcile cron and FastAPI background tasks all touch `schedule.json`,
`.reconcile.state.json` and `cache/values.json`, so every store does `flock` +
atomic rename and reconcile is serialized end to end.

## The app

`create_app()` (`src/ndif/services/dashboard/backend/app.py`) builds CORS for the
Vite dev origins only, six routers, `/api/health`, and the SPA fallback. Static
serving is registered last: `dist/assets` mounts at `/assets`, then a catch-all
`GET /{full_path:path}` returns the file if it exists and `index.html` otherwise
(so `/schedule` survives a hard refresh). FastAPI matches in registration order, so
every `/api/*` route wins. With no `dist/index.html` the catch-all isn't registered
and `GET /` returns a JSON hint. `start.sh` gives uvicorn the module-level
`app = create_app()`.

## Endpoints

Everything except `/api/health` and the auth routes depends on `require_auth`, a
no-op under `NDIF_DASHBOARD_DEV_MODE=true`.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/api/health` | none | `{"ok": true, "dev_mode": …}` |
| `POST` | `/api/auth/login` | none | verify username + bcrypt hash, set the session cookie |
| `POST` | `/api/auth/logout` | none | delete the cookie |
| `GET` | `/api/auth/me` | cookie | `{username, dev_mode}`; a 401 sends the SPA to `/login` |
| `GET` | `/api/monitor/{connected,models,cluster}` | yes | the matching monitor JSONL entries, concatenated |
| `GET` / `POST` | `/api/schedule` | yes | list / create (201; canonicalizes, then background-reconciles) |
| `GET` / `PUT` / `DELETE` | `/api/schedule/{id}` | yes | one event (404 if absent) / update / delete (204); writes background-reconcile |
| `GET` | `/api/status` | yes | controller status via Ray, deduped, aggregated, pinned-tagged |
| `POST` | `/api/deployments/{deploy,evict,restart}` | yes | one model per call; deploy also bumps the autocomplete cache |
| `GET` | `/api/cache` | yes | the autocomplete MRU lists |

Two files, confusingly named: `routers/deploy.py` serves only `/api/status`;
`routers/deployments.py` owns `/api/deployments/*`. `routers/cache.py` shares the
bare `/api` prefix.

### `/api/status`

`status_endpoint` (`src/ndif/services/dashboard/backend/routers/deploy.py`) calls
`ndif_client.status()` — a direct Ray RPC to the controller actor, **not** the API's
cached `/status`, because the Deployments view reloads right after every action and
the cache would show the previous level for up to `NDIF_STATUS_TTL_S`. It then:

1. **Drops HF-cache shadows.** The controller lists the local HF cache as COLD
   entries, so a repo fetched under two casings appears twice; a COLD entry is
   dropped when a non-COLD one exists for the same case-insensitive
   `(repo_id, revision)`.
2. **Aggregates replicas.** `_aggregate_by_model_key` collapses one-entry-per-replica
   into one card per `model_key` with a `replicas` array; card fields take the best
   value across siblings (`HOT > WARM > COLD`, `RUNNING > DEPLOYING > NOT_STARTED >
   UNHEALTHY`, `pinned`/`trusted` OR'd). COLD entries pass through keyed by app name.
3. **Tags pinned.** `pinned` is OR'd with "this `model_key` matches an active
   schedule event", using the key stamped on the event at write time.

### Error translation

`_call` (`src/ndif/services/dashboard/backend/routers/deployments.py`) wraps every
`ndif_client` call: `NDIFConnectivityError` → 503, `ValueError` → 400, anything else
→ 500 with `f"{type(e).__name__}: {e}"` — the broad catch is what gets an HF
`OSError` on a gated repo into the toast readably. `_log_action` logs the lib's
transcript and per-model failures, since the deploy lib returns 200 even when
individual models fail.

### Dashboard deploys are deployment-trusted

`DeployRequest.trusted` defaults to `True`, and `_spec_from_event`
(`src/ndif/services/dashboard/jobs/reconcile.py`) hardcodes it for scheduled
deploys. It becomes `DeploymentConfig.trusted` and then `trust_remote_code=` for both
the controller's size estimate and the actor's model load — so a checkpoint with
custom modeling code runs that code in the model actor.

A *different* flag shares the name: `BackendRequestModel.trusted` decides whether a
request's block runs in-process or in a runner, and is set per request at ingress
(see [Auth and limits](../concepts/auth-and-limits.md)) — never from the dashboard's
deploy flag.

## Config and auth

`Settings` (`src/ndif/services/dashboard/backend/config.py`) is a pydantic
`BaseSettings` with `env_prefix="NDIF_DASHBOARD_"`. Derived paths (`logs_dir`,
`schedule_path`, `reconcile_state_path`, `monitor_config_path`, `cache_path`) are
properties off `data_dir`, and `get_settings()` is `lru_cache`d and creates the
directories — so env changes after the first call don't take effect.

`src/ndif/services/dashboard/backend/auth.py` has no user model.
`hash_password` / `verify_password` are bcrypt with an explicit 72-byte truncation
(`_to_bytes`; bcrypt 4.x raises rather than truncating). `issue_session_token`
signs `{"u": username}` with `itsdangerous.URLSafeTimedSerializer(secret,
salt="ndif-dashboard")`; `parse_session_token` checks it with `max_age =
session_ttl_days * 86400`. Nothing is stored server-side — the cookie *is* the
session, so rotating the secret invalidates every session. `require_auth` returns
`settings.username` under dev mode, else reads the `ndif_dashboard_session` cookie
and 401s if it is missing, unsigned, expired, or names a different user. `_cli`
implements `python -m ndif.services.dashboard.backend.auth hash <password>`, the
supported way to produce `NDIF_DASHBOARD_PASSWORD_HASH`.

## schedule_store

`src/ndif/services/dashboard/backend/schedule_store.py`: two Pydantic models and a
class over one JSON file. `ScheduleEventIn` is the write payload (`title`,
`checkpoint`, `start`, optional `end`, the deployment knobs), with a validator
rejecting `end <= start`. `ScheduleEvent` adds `id`, timestamps, `last_status`,
`last_error`, and makes `model_key` required — every persisted event has been
canonicalized.

Every method wraps its read-modify-write in `_locked`, an `fcntl.flock(LOCK_EX)` on a
**sidecar** `schedule.json.lock`, because `_write_json` replaces the data file via
`mkstemp` + `os.replace` and a lock on the original inode would be orphaned by the
rename. `_read_json` returns `{"events": []}` for a missing *or* corrupt file — the
store never raises on a bad file. `mark_status` is reconcile's write path. Activity
is `_is_active`: `start <= when and (end is None or when < end)`; `filter_active` is
exported so reconcile and `/api/status` share it.

## cache_store

`src/ndif/services/dashboard/backend/cache_store.py` backs the autocomplete
dropdowns: MRU lists for the `FIELDS` (`repo_id`, `task`, `actor_class`,
`envoy_class`), capped at `MAX_ENTRIES_PER_FIELD` (200), in `cache/values.json` with
the same sidecar-flock + rename discipline. `add_from_deploy_result` is the real
entry point: it pairs input specs to the deploy results by `checkpoint`, **skips
anything carrying an `error`**, and records the canonical `repo_id` parsed from the
returned `model_key` rather than what the user typed, so `…-8b` and `…-8B` collapse.

## log_reader and the monitor JSONL format

`src/ndif/services/dashboard/backend/log_reader.py` `parse_log_files(log_dir,
pattern)` globs, sorts by filename (files are `*_YYYY-MM-DD.log`), and parses one JSON
object per line, skipping blanks and unparseable lines. No pagination or filter: the
router returns the whole retention window and the SPA slices client-side. The shapes,
written by `jobs/monitor.py`:

```jsonc
// connected_*.log — one per tick; status is "ok" or the reason
{"timestamp": "2026-07-22T18:00:00+00:00", "status": "ok"}
// models_*.log — one per trace pass; "degraded" if any failed
{"timestamp": "...", "status": "ok", "ok": 3, "total": 4,
 "results": [{"model": "openai-community/gpt2", "status": "ok", "latency_s": 1.23},
             {"model": "…", "status": "error", "latency_s": 4.5, "error": "...",
              "traceback": "...", "request_id": "..."}]}
```

A failed result carries two error fields on purpose: `error` is `summarize_error`'s
one line, which the Discord ping renders, and `traceback` is the full local stack,
whose last frame carries the remote one. `request_id` is present whenever the job
reached the API, so the failure can be found in the API logs. `cluster_*.log` adds
`nodes`, `total_gpus`, `total_memory_bytes`, `available_memory_bytes` and a
`node_details` array. Per-model `status` is `ok`, `error`, `load_error` (the nnsight
wrapper couldn't be rebuilt) or `timeout`.

## ndif_client

A thin adapter over `src/ndif/cli/lib/`. The lib functions stream progress through an
`on_message` callback; `_with_logs` injects a list-appending one and attaches the
lines as `result["logs"]`, so an HTTP response or cron log carries the transcript the
CLI prints. `deploy`/`evict`/`restart` are wrapped, `status` is re-exported as is,
`evict_all(**kw)` is `evict(evict_all=True, **kw)`. Ray connection and address
resolution live in the lib — see [CLI internals](cli-internals.md).

## jobs/monitor.py

A one-shot script. `main()` arms a `SIGALRM` for `SCRIPT_TIMEOUT` (480 s) that
`os._exit(2)`s a wedged run, then takes a non-blocking flock on `logs/.monitor.lock`
and exits if held (`acquire_lock`) — a slow tick skips rather than overlaps.
`probe_health` does `GET /connected` then `GET /status`, calling the deployment down
for an unreachable API, a failed `/connected`, an unreachable `/status`, **or zero
HOT models** — `/connected` alone is too shallow, since the Ray client can be alive
while the controller is wedged. A cluster snapshot is written whenever `/status`
answered. If healthy and `model_check_due` (first run, recovery, or
`--model-interval`, default 7200 s, elapsed), it runs one remote trace per HOT model.
Then it writes the connectivity datapoint, applies the up/down transition
(`record_status_transition`), saves `.state.json`, and rotates logs.

`_run_trace` needs nnsight in the dashboard environment. It rebuilds the wrapper from
the full `model_key` via `Remotable.from_model_key` (so VLM and PEFT deployments are
exercised under their own class) and runs `model.trace("Hello", remote=True)`. It
points `CONFIG.API.HOST` at `--api-host or --url`; otherwise nnsight's default public
host is used and the probe silently checks a different deployment. Each trace runs in
a one-worker pool so `--model-timeout` (default 60 s) can abandon a hung request
(`check_model`).

Discord notification is edge-triggered: `notify_if_failures_changed` posts only when
the sorted failed-model set differs from the one in `.state.json`, and
`notify_status` posts on up→down, down→up and still-down. `notify_model_failures`
adds models until Discord's 2000-character limit, each line carrying
`summarize_error`'s one-liner (truncated to 120 characters) and the `request_id`;
full tracebacks stay in `models_*.log`.

## jobs/reconcile.py

`reconcile_once(force=False)` is the entry point for cron (`main()`) and the FastAPI
background task: `_reconcile_lock` around `_reconcile_locked`.

`_reconcile_lock` is a **blocking** exclusive flock on `.reconcile.state.json.lock` —
blocking because each schedule write queues its own background reconcile while the
2-minute cron runs alongside, and unserialized passes would read the same stale
`prev_model_keys` and leak deployments or drop evictions. The critical section spans
read-diff-act-write including the deploy/evict RPCs, so it can be held for tens of
seconds. The diff:

```python
new_keys = {e.model_key: e for e in filter_active(store.list())}
prev_keys = set(state.get("prev_model_keys") or [])
to_evict = sorted(prev_keys - set(new_keys))
hot_keys = _fetch_hot_model_keys()            # live, via Ray
to_deploy_events = [ev for mk, ev in new_keys.items()
                    if mk not in prev_keys or mk not in hot_keys]
```

`_fetch_hot_model_keys` returns `None` on any Ray-side error, and `None` skips the
pass unless `--force` — acting blind would make every scheduled model look drifted
and stack a second pinned replica on the one already serving.

Then: evict first, one `deploy(specs, sync=False)` (additive — `sync` would evict
everything not in the list, fighting ad-hoc deploys), and persist state even if
deploy raises after a successful evict, so the next tick doesn't re-evict. Per-event
`last_status`/`last_error` come from the deploy result matched by `checkpoint`;
failures go to Discord via the `schedule_failed` template; successful specs are
mirrored into the autocomplete cache. `_spec_from_event` holds the schedule's policy:
`pinned=True`, `trusted=True`.

`_trigger_reconcile` (`src/ndif/services/dashboard/backend/routers/schedule.py`)
imports `reconcile_once` lazily (it pulls in Ray and nnsight) and swallows every
exception with a logged traceback — a failed background reconcile must not turn a
successful schedule write into a 500, and the cron retries.

## jobs/report.py

A daily digest posted to Discord (cron default `0 0 * * *`), to `discord_report_webhook` when set and to `discord_webhook` otherwise. Uptime comes from the
monitor's `connected_*.log`, so it cannot disagree with the alerts already sent; usage
(request counts, outcomes, users, models, latency percentiles, bytes) comes from
InfluxDB. If InfluxDB is unreachable the usage block is reported unavailable and the
uptime report still goes out.

## Dependencies

The `dashboard` extra is web-app only (`fastapi`, `uvicorn[standard]`,
`pydantic-settings`, `itsdangerous`, `bcrypt`, `python-multipart`, `requests`). The
crons need more: reconcile drives `cli/lib`, so it needs the `ray` extra (Ray pinned
in `requirements.txt` to match the ray service); the monitor's probe needs nnsight
plus `peft` / `torchvision` for PEFT and VLM checkpoints; the report reads InfluxDB.
The image installs every extra, so the container has all of it.

## Related

[Dashboard](../operating/dashboard.md) for running it and the schedule model;
[Dashboard frontend](dashboard-frontend.md) for the SPA and how its build ships;
[CLI internals](cli-internals.md) for the lib functions every mutation goes through;
[Controller internals](controller-internals.md) for the other side of the Ray RPC.
