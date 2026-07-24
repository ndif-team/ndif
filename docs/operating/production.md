---
title: Going to Production
one_liner: What has to change before an NDIF faces users — auth, dashboard credentials, object store, multi-node Ray, sizing, timeouts, retention — and what the code honestly does not provide.
tags: [operating, auth, ray, telemetry, gotchas]
related: [docs/operating/quickstart.md, docs/operating/compose-stack.md, docs/operating/configuration.md, docs/operating/observability.md, docs/operating/troubleshooting.md, docs/runbooks/enable-auth.md, docs/runbooks/add-a-gpu-node.md, docs/runbooks/model-oom-on-deploy.md, docs/concepts/auth-and-limits.md, docs/concepts/sandbox-execution.md, docs/concepts/deployments-and-eviction.md, docs/reference/env-vars.md, docs/reference/ports.md, docs/gotchas/gpu-and-memory.md]
sources: [docker/docker-compose.yml, src/ndif/services/api/auth.py, src/ndif/services/ray/start.sh, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/api/queue/config.py, src/ndif/common/providers/objectstore.py, src/ndif/services/dashboard/backend/config.py, docker/postgres/init.sql]
---

# Going to Production

## What this covers

The shipped `docker/docker-compose.yml` is a **development** stack: zero-config,
single-host, and deliberately disposable. It is not a production template, and
this page does not try to turn it into one. What it covers is the **configuration**
delta — the settings you must change before anyone else can reach the API — plus
an honest list of what NDIF does not provide.

Actual deployment specifics are out of scope. **Bring your own TLS termination,
orchestration, secret store, and infrastructure.** A real deployment is your own
orchestration wrapping the same images and the same `NDIF_*` environment; there is
no production compose file, and nothing here prescribes a proxy config, a tuning
number, or a topology. Each config item below points at the code that makes it
true.

## 1. Turn on auth — this is the one that matters

`NDIF_POSTGRES_URL` is commented out in the compose file
(`docker-compose.yml:156`). With it empty, `PostgresProvider.enabled()` is False,
`verify_api_key` returns `None`, and `validate_request` defaults
**`request.trusted` to `True`** for any request that doesn't set it
(`src/ndif/services/api/auth.py:180`).

`trusted` is the most consequential flag in the system:

- A trusted request's traced block runs **in-process inside the model actor**,
  in the same process as the weights — no runner subprocess, no socket
  (`src/ndif/services/ray/sandbox/model.py`). Whatever Python the caller
  submitted executes with the actor's privileges.
- The same flag becomes **`trust_remote_code=`** when the model is loaded
  (`cluster/cluster.py:169`, `controller/controller.py:280`), so a request naming
  an arbitrary Hugging Face repo can run that repo's code on your GPU node.

So: an unauthenticated NDIF is not "an NDIF anyone can use", it is "an NDIF
anyone can run arbitrary code on". Enable auth first, then decide who gets the
`trusted` tag.

Enabling it means: set `NDIF_POSTGRES_URL` on the **api** service (the schema in
`docker/postgres/init.sql` already matches what `verify_api_key` queries), point
it at a Postgres you control with a real password rather than `admin`/`admin`,
and create users and keys — API keys are UUIDs, and a key is a row in `keys`
joined to a `users` row (`auth.py:56-62`). Grant the `trusted` user_tag only to
keys you would hand your own shell to; grant `priority` to keys that should jump
the queue. Full walkthrough in `docs/runbooks/enable-auth.md`.

Two related knobs worth setting at the same time: `NDIF_MIN_NNSIGHT_VERSION` and
`NDIF_MIN_PYTHON_VERSION` reject client versions you can't serve
(`src/ndif/services/api/versioning.py:23-24`); unset, there is no gating at all.

> **Note on the sandbox:** with auth on and a key that lacks the `trusted` tag,
> user code runs in a **separate process** from the model actor, interleaved with
> the forward pass over a Unix socket, and a fresh runner is started and stopped
> per request. It is process separation, not a hardened jail — no namespaces,
> seccomp, rlimits, or filesystem isolation today. Size your threat model
> accordingly; see `docs/concepts/sandbox-execution.md`.

## 2. Lock down the dashboard

Compose sets `NDIF_DASHBOARD_DEV_MODE: "true"` (`docker-compose.yml:192`), which
makes `require_auth` return the configured username without checking a cookie
(`dashboard/backend/auth.py:73`). The dashboard can deploy, evict and restart
models — it is a control plane, not a viewer.

```bash
# Remove NDIF_DASHBOARD_DEV_MODE entirely, then:
python -m ndif.services.dashboard.backend.auth hash '<your password>'
```

Set `NDIF_DASHBOARD_USERNAME`, the resulting bcrypt hash as
`NDIF_DASHBOARD_PASSWORD_HASH`, and a random 32+ byte
`NDIF_DASHBOARD_SESSION_SECRET` — the default is the literal string
`change-me-please-this-is-not-secure` (`dashboard/backend/config.py:42`), and
anyone who knows it can forge a session cookie. `NDIF_DASHBOARD_SESSION_TTL_DAYS`
(7) bounds how long a stolen cookie is useful.

Note that dashboard deploys hard-code `trusted: True` on the deployment config —
an admin action, by design. Also give the dashboard container a durable
`NDIF_DASHBOARD_DATA_DIR` volume; its schedule and monitor state live there.

## 3. Real object storage, reachable from two networks

`NDIF_OBJECT_STORE_ACCESS_KEY` / `_SECRET_KEY` default to `minioadmin` on both
the server and the MinIO container. Replace them, and put the bucket somewhere
durable — the dev MinIO writes to the container filesystem and loses everything
on `just down`.

> **Gotcha — the single most common misconfiguration.** There are two endpoint
> variables and they are not interchangeable
> (`common/providers/objectstore.py:9-18`). `NDIF_OBJECT_STORE_URL` is what the
> *server* uploads through. `NDIF_OBJECT_STORE_PUBLIC_URL` is what presigned GET
> URLs are **signed with** — and a presigned URL is an HMAC over the request
> including the host, so signing with an internal hostname produces a link your
> users cannot resolve, let alone fetch. Set `PUBLIC_URL` to the address a client
> on the public internet will hit. Leave `NDIF_OBJECT_STORE_URL` empty only if
> you want real AWS S3 for `NDIF_OBJECT_STORE_REGION`.

Both variables must be set identically on `api` and `ray` — the model actor
uploads and signs, the API serves `/response/{id}` from the same bucket.

Presigned URLs expire after **one hour**
(`objectstore.py:160-167`), which is not currently env-configurable. Result blobs
are never deleted by NDIF; if you don't want the bucket to grow forever, add a
lifecycle rule on the store itself.

## 4. Multi-node Ray

One variable decides a node's role. `ray/start.sh:27` reads
`NDIF_RAY_HEAD_ADDRESS`: empty ⇒ this node runs `ray start --head` and then
launches the NDIF controller (`start.sh:50-70`); set ⇒ it waits for that
`HOST:PORT` to accept TCP and joins as a worker (`start.sh:72-89`).

```bash
# Head node
NDIF_RAY_HEAD_PORT=6385                  # the GCS port workers join
NDIF_REDIS_URL=redis://redis.internal:6379

# Worker node
NDIF_RAY_HEAD_ADDRESS=ray-head.internal:6385
NDIF_REDIS_URL=redis://redis.internal:6379
```

Both roles run the same image; only the environment differs. `NDIF_RAY_ADDRESS`
is unrelated — it is the `ray://` *client* address the API, dashboard and CLI use
to reach the head, and says nothing about head-vs-worker.

The controller runs on the head and only the head: it is pinned with
`resources={"head": 1}` (`controller/controller.py:527`), and `resources.py` only
advertises `head=10` when invoked with `--head`. Each node also advertises
`cuda_memory_bytes` and `cpu_memory_bytes`, which the controller reads back for
placement (`resources.py:37-49`). A node with no GPU is ignored entirely
(`cluster/cluster.py:91-92`).

**Ports that must be reachable from every worker to the head:**

| Port | Env var | What |
|---|---|---|
| 6385 | `NDIF_RAY_HEAD_PORT` | Ray GCS — the join address. The CLI and `start.sh` both default to 6385, offset from Redis's 6379. |
| 8076 | `NDIF_RAY_OBJECT_MANAGER_PORT` | Plasma object transfer between nodes |
| 52366 | `NDIF_RAY_DASHBOARD_GRPC_PORT` | Dashboard agent gRPC |

Plus Ray's own node-manager and worker port ranges, which `start.sh` does not
pin — consult Ray's port documentation rather than guessing. From the API and
dashboard hosts, 10001 (`ray://` client) must reach the head. Nothing in this
list belongs on a public interface: anyone who can reach 10001 can run arbitrary
code on the cluster. See `docs/reference/ports.md` and
`docs/runbooks/add-a-gpu-node.md`.

Prometheus's single static `ray:8080` target only covers one node. A multi-node
cluster needs Ray's own metrics service discovery instead — configure that in your
own Prometheus.

## 5. Sizing

A few config knobs decide how the cluster uses its hardware. Set them for your
node, not the dev defaults; the detail lives in the runbooks and
`docs/reference/env-vars.md`.

- **GPU memory is estimated, not measured.** The controller sizes a model from a
  meta-device estimate plus padding, and never reads the card's real free memory.
  Long-context or heavily-batched workloads can pass placement and then OOM.
  `NDIF_DEFAULT_PADDING_FACTOR` / `NDIF_DEFAULT_PADDING_BIAS` set the global
  headroom, and a per-model `padding_factor` raises it for just the models that
  need it. See `docs/runbooks/model-oom-on-deploy.md` and
  `docs/gotchas/gpu-and-memory.md`.
- **The WARM cache is CPU RAM.** `NDIF_MODEL_CACHE_PERCENTAGE` (default `0.9`) is
  the fraction of the node's **total system RAM** the controller may use to hold
  evicted models off-GPU (`cluster/cluster.py:106-108`) — not a GPU setting. Lower
  it on a node that runs anything else.
- **Replicas are the unit of concurrency.** A replica serves one request at a
  time; `NDIF_AUTOSCALING_MAX_REPLICAS` caps how many per model autoscaling adds.
- **Disk.** The `ray` service needs a persistent Hugging Face cache and, for gated
  repos, `HF_TOKEN`. The dev compose already bind-mounts the host HF cache and
  passes `HF_TOKEN` through; a non-compose deployment must arrange both itself.
  `NDIF_RAY_TEMP_DIR` (`/tmp/ray`) needs real space.

## 6. Timeouts and retention

Several timeouts (`NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS`,
`NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS`, `NDIF_API_TIMEOUT`,
`NDIF_POSTGRES_COMMAND_TIMEOUT_S`, the `/status` and `/env` cache timeouts) carry
defaults tuned for a single-host dev stack. Review them against your workload —
each is documented with its default in `docs/reference/env-vars.md`.

The dev stack is **deliberately disposable**: it keeps no telemetry, Redis, blob,
or Postgres volumes, so metrics, results, and the keys DB vanish on a container
recreate (only the host HF cache mount and the `dashboard_data` volume persist).
That is fine for development and unacceptable for a deployment you rely on — a
real deployment brings its own durable volumes and retention policy for anything
it needs to keep. See `docs/operating/observability.md`.

## What NDIF does not provide

State this plainly to yourself before you deploy:

- **No TLS.** Gunicorn binds plain HTTP on `0.0.0.0:8001`
  (`api/gunicorn_conf.py:29`); there are no certificate options anywhere. API keys
  travel in the `ndif-api-key` header. Terminate TLS yourself and never expose
  8001 directly — that termination and the network isolation around it are yours
  to build.
- **No rate limiting or quotas.** The only per-key behaviors implemented are the
  `trusted` and `priority` tags. A single key can submit unlimited requests.
- **No multi-tenancy.** All users share the same model deployments and the same
  GPUs. There is no per-user isolation beyond the trusted/untrusted execution
  fork, and no accounting.
- **No secret management.** Credentials are environment variables. Bring your own
  secret store.
- **No CI and no compatibility guarantees.** The only test suite is the
  live-server one under `tests/`, which skips unless a stack is reachable at
  `localhost:8001`. This is v0.0.1; there is no stable public API.
- **No horizontal API tier.** `NDIF_API_WORKERS` scales gunicorn workers in one
  process group, and the queue dispatcher is started exactly once by the gunicorn
  master (`gunicorn_conf.py:61-66`). Running two API *containers* against one
  Redis would run two dispatchers — not a configuration the code anticipates.

## Pre-flight checklist

| # | Check | Where | Done when |
|---|---|---|---|
| 1 | `NDIF_POSTGRES_URL` set on the API, keys created, `trusted` granted deliberately | api | `GET /whoami` with a key returns your email; without one, `/request` 401s |
| 2 | Postgres password is not `admin`, and the DB is not published to the world | postgres | — |
| 3 | `NDIF_DASHBOARD_DEV_MODE` removed; username, bcrypt hash, random session secret set | dashboard | The dashboard prompts for a login |
| 4 | Object-store credentials replaced; bucket durable | api, ray | — |
| 5 | `NDIF_OBJECT_STORE_PUBLIC_URL` resolves and fetches **from a client machine** | api, ray | A remote trace's result downloads from off-host |
| 6 | `NDIF_REDIS_URL` explicitly set on **every** service — never left at localhost | all | `ndif info` in each container shows the right host |
| 7 | Head/worker split correct: `NDIF_RAY_HEAD_ADDRESS` set on workers only | ray | `ndif status` lists every GPU node |
| 8 | 6385 / 8076 / 52366 open between nodes; 10001 and 8265 **not** public | network | — |
| 9 | TLS terminating proxy in front of 8001; 8001 not directly exposed | network | `https://` endpoint answers `/ping` |
| 10 | Persistent volumes for HF cache, MinIO, Postgres, Loki, Influx, dashboard data | all | Survives a container recreate |
| 11 | `HF_TOKEN` set on `ray` if you serve gated checkpoints | ray | The gated model deploys |
| 12 | `NDIF_ENVIRONMENT` set to something other than `dev` | all | Grafana can separate this deployment |
| 13 | `NDIF_MODEL_CACHE_PERCENTAGE` sized for what else runs on the node | ray | — |
| 14 | `NDIF_AUTOSCALING_MAX_REPLICAS` and padding factors reviewed per model | ray | — |
| 15 | `NDIF_MIN_NNSIGHT_VERSION` set to the oldest client you will support | api | An older client gets a clear 400 |
| 16 | Durable volumes and a retention policy for anything you must keep | telemetry, store | Survives a recreate; disk doesn't fill unbounded |

## Related

- `docs/runbooks/enable-auth.md` — the auth switch, end to end.
- `docs/runbooks/add-a-gpu-node.md` — joining a worker to an existing head.
- `docs/operating/configuration.md` — how these variables layer and where they're read.
- `docs/reference/env-vars.md` — every variable and its default.
- `docs/reference/ports.md` — the full port map, published and internal.
- `docs/concepts/auth-and-limits.md` — what an API key actually carries.
