---
title: Dashboard Frontend
one_liner: The dashboard's Vue 3 SPA — file layout, the fetch wrapper and auth guard, per-view data flow, the Vite dev proxy, and how the committed built output reaches a deployed container.
tags: [internals, dev, dashboard, gotchas]
related: [docs/developing/dashboard-internals.md, docs/operating/dashboard.md, docs/developing/contributing.md, docs/developing/repo-layout.md]
sources: [src/ndif/services/dashboard/frontend/package.json, src/ndif/services/dashboard/frontend/vite.config.ts, src/ndif/services/dashboard/frontend/src/api.ts, src/ndif/services/dashboard/frontend/src/router.ts, src/ndif/services/dashboard/frontend/src/stores/auth.ts, src/ndif/services/dashboard/frontend/src/views, src/ndif/services/dashboard/backend/app.py, pyproject.toml, docker/Dockerfile, .gitignore]
---

# Dashboard Frontend

## What this covers

`src/ndif/services/dashboard/frontend/` — the Vue 3 SPA the dashboard backend
serves as static files. How it's laid out, how it talks to the backend, how to run
it against a live backend, and how the built output does (and doesn't) reach a
deployed container. The backend it calls is documented in
`docs/developing/dashboard-internals.md`.

See the packaging section for the shipping model: **the built frontend is
committed** (`frontend/dist/` is tracked despite the blanket `dist/` ignore), so a
fresh `just up` serves a working UI with no host-side build — you only rebuild when
you change the frontend.

## Stack and layout

Vue 3 `<script setup>` + Vite + TypeScript, Pinia for the auth store, `vue-router`
in history mode, Chart.js for the latency chart. No UI framework — one hand-rolled
`src/styles/theme.css`, with a light/dark toggle that writes
`document.documentElement.dataset.theme` and `localStorage` (`App.vue`).

```
frontend/  index.html  package.json  vite.config.ts  tsconfig.json
  src/ main.ts          # createApp + pinia + router, mount #app
       App.vue          # header/nav chrome (hidden on /login), theme + logout
       router.ts        # routes + the global auth guard
       api.ts           # fetch wrapper, ApiError
       deploy.ts        # DEFAULT_ENVOY_CLASS + CacheValues (shared types)
       stores/auth.ts             composables/useCache.ts
       views/{Login,Monitor,Deployments,Schedule}View.vue
       components/AutocompleteInput.vue
       components/monitor/{ConnectivityCalendar,LatencyChart,ModelTimeline,ClusterCard}.vue
       components/deployments/{DeploymentCard,DeployModal}.vue
       components/schedule/{MonthCalendar,EventModal}.vue
```

`deploy.ts` exists because Vue's `<script setup>` blocks non-type exports — shared
constants like `DEFAULT_ENVOY_CLASS` and the `CacheValues` type have to live
outside a `.vue` file or every view keeps its own copy.

## api.ts and auth

`api.ts` is a ~60-line fetch wrapper — `api.get/post/put/del` — that always sends
`credentials: 'include'` (the session cookie is HttpOnly, so JS never sees a
token), sets `Content-Type: application/json`, maps `204` to `undefined`, and on a
non-2xx throws an `ApiError` carrying `status` and the parsed `detail`. Views
catch it to render a toast, which is why the backend takes care to put a readable
string in `detail` rather than letting FastAPI return a bare
`Internal Server Error` — see *Error translation* in
`docs/developing/dashboard-internals.md`. A few call sites fall back to
`e.message` when `detail` is an object, since a 422 validation error's `detail` is
a list and would otherwise render as `[object Object]`.

`stores/auth.ts` is the only Pinia store: `username`, `devMode`, `checked`.
`refresh()` calls `GET /api/auth/me`, treats a 401 as "logged out" and re-raises
anything else. `router.ts`'s `beforeEach` calls `refresh()` once per session, sends
an unauthenticated user to `/login?next=<path>`, and bounces an authenticated one
off `/login`. That guard is the entire auth integration. Under
`NDIF_DASHBOARD_DEV_MODE=true` `/api/auth/me` always answers, so the login view is
unreachable.

Routes: `/` and any unknown path redirect to `/deployments`; `/login` is the only
one marked `meta: { public: true }`. All views are lazy `import()`s, which is what
produces the per-route chunks under `dist/assets`.

## Views and data flow

- **`MonitorView`** fetches `/api/monitor/{connected,models,cluster}` in parallel
  on mount and re-polls every 5 minutes, computing uptime and average latency
  client-side before handing the raw arrays to `ConnectivityCalendar` (30-day
  grid), `LatencyChart` (Chart.js, colors read from CSS custom properties so it
  follows the theme), `ModelTimeline` (per-model 2-hour slots, 2h15m grace before
  a slot reads as a gap) and `ClusterCard`. Everything on this page is history
  from the monitor cron's JSONL files; nothing here touches Ray.
- **`DeploymentsView`** is the only live view: `load()` GETs `/api/status` and
  renders one `DeploymentCard` per model with level/search/sort filters, and card
  actions POST to `/api/deployments/{deploy,evict,restart}` then `load()` again.
  Two behaviors worth knowing — a WARM card redeploys by passing the existing
  `model_key` (short-circuiting the deploy lib's HF canonicalization) with no
  modal while a COLD card opens `DeployModal`; and an in-flight deploy sits in a
  local `pending` list rendered as a placeholder card until a HOT/WARM deployment
  with the same `(repo_id, revision)` appears or a 5-minute TTL expires, so a
  deploy the controller no-ops doesn't read as success.
- **`ScheduleView`** GETs `/api/schedule`, renders `MonthCalendar`, and
  creates/updates/deletes through `EventModal`, whose "forever" option sends
  `end: null`.
- **`LoginView`** posts to `/api/auth/login` and follows `?next`.

`composables/useCache.ts` owns the `/api/cache` autocomplete state shared by
`DeployModal` and `EventModal`; it fetches on mount and swallows errors, since
autocomplete is a convenience and no deploy depends on it. `AutocompleteInput`
replaces a native `<datalist>` (OS-skinned, clashes with the theme):
case-insensitive substring filter, 50 visible max, arrow/Enter/Esc keys.

## Adding a view

Add `views/MyView.vue`, register a route in `router.ts` (omit
`meta: { public: true }` so the guard covers it), add a `RouterLink` in `App.vue`'s
`<nav class="tabs">`, and call the backend through `api.*`. No store wiring is
needed unless the state is shared across views, in which case follow
`composables/useCache.ts`. If the view needs a new endpoint, add it to a router
under `backend/routers/` with `Depends(require_auth)` — the SPA assumes every
`/api/*` route except the auth ones is authenticated.

## Dev loop

```bash
cd src/ndif/services/dashboard/frontend
npm install
npm run dev        # Vite on :5173, proxies /api -> http://localhost:8081
```

Run the backend separately (`ndif start dashboard`), ideally with
`NDIF_DASHBOARD_DEV_MODE=true` so you skip the login round-trip.
`vite.config.ts` sets `cookieDomainRewrite: 'localhost'` on the proxy so the
backend's session cookie survives the origin change, and the backend's CORS
middleware allowlists exactly `http://localhost:5173` and `http://127.0.0.1:5173`
with `allow_credentials=True`. `@` is aliased to `src/`.

In dev you do not need `dist/` at all — Vite serves the UI and the backend only
answers `/api/*`. The committed `dist/` (see below) is what a non-dev deployment
serves.

## Build and packaging

`npm run build` is `vue-tsc --noEmit && vite build` — a type error fails the build
— emitting `frontend/dist/` with hashed assets under `dist/assets`, which is what
the backend mounts (`backend/app.py:61`).

> **The built SPA is committed.** `frontend/dist/` is tracked despite the blanket
> `dist/` rule (`.gitignore:9`), because `.gitignore:13-15` explicitly un-ignores
> `src/ndif/services/dashboard/frontend/dist/`. So `git ls-files` returns the
> hashed assets, `docker/Dockerfile`'s `COPY src/ ./src/` carries them into the
> image, and a wheel picks them up as package-data — no host-side `npm` is needed.
> `docker/Dockerfile` still contains no `npm` or `node`; it doesn't need to.

`pyproject.toml`'s `[tool.setuptools.package-data]` ships `"frontend/dist/*"` and
`"frontend/dist/**/*"` for the `ndif.services.dashboard` package, and
`NDIF_DASHBOARD_FRONTEND_DIST` defaults to that same directory
(`backend/config.py:42`). Both halves now agree — the committed `dist/` is present
for the package-data entry to ship. The upshot:

- **`just up` from a fresh clone serves the UI.** The backend's `dist` check
  passes and the SPA catch-all is registered; `GET /` serves `index.html`.
- **A wheel built from a clean checkout carries the assets**, so it serves a UI
  wherever it's installed.

You only rebuild when you change the frontend. Run the build on the host, then
rebuild the image so the new assets are copied in:

```bash
cd src/ndif/services/dashboard/frontend && npm ci && npm run build && cd -
just build dashboard && just up dashboard
```

## Related

- `docs/developing/dashboard-internals.md` — the FastAPI backend, its routers, the
  stores, and the cron jobs this SPA drives.
- `docs/operating/dashboard.md` — running the dashboard, auth setup, and the same
  build prerequisite from an operator's angle.
- `docs/developing/contributing.md` — house conventions for changes to this repo.
