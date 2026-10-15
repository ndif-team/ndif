---
title: Going to Production
one_liner: What has to change before an NDIF faces users — auth, the sandboxed actor class, dashboard credentials, object store, multi-node Ray, sizing, timeouts, retention — and what the code does not provide.
tags: [operating, auth, ray, telemetry, gotchas]
related: [docs/operating/quickstart.md, docs/operating/compose-stack.md, docs/operating/configuration.md, docs/operating/observability.md, docs/operating/troubleshooting.md, docs/runbooks/enable-auth.md, docs/runbooks/add-a-gpu-node.md, docs/runbooks/model-oom-on-deploy.md, docs/concepts/auth-and-limits.md, docs/concepts/sandbox-execution.md, docs/concepts/deployments-and-eviction.md, docs/reference/env-vars.md, docs/reference/ports.md]
sources: [docker/docker-compose.yml, src/ndif/services/api/auth.py, src/ndif/services/api/app.py, src/ndif/services/ray/start.sh, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/evaluator.py, src/ndif/services/ray/sandbox/model.py, src/ndif/services/ray/tp/model.py, src/ndif/services/api/queue/config.py, src/ndif/common/providers/objectstore.py, src/ndif/services/dashboard/backend/config.py, docker/postgres/init.sql]
---

# Going to Production

## What this covers

The shipped `docker/docker-compose.yml` is a **development** stack: zero-config,
single-host, and deliberately disposable. It is not a production template, and
this page does not try to turn it into one. What it covers is the **configuration**
delta — the settings you must change before anyone else can reach the API — plus
a plain list of what NDIF does not provide.

Actual deployment specifics are out of scope. **Bring your own TLS termination,
orchestration, secret store, and infrastructure.** A real deployment is your own
orchestration wrapping the same images and the same `NDIF_*` environment; there is
no production compose file, and nothing here prescribes a proxy config, a tuning
number, or a topology.

## 1. Turn on auth, and run untrusted code out of process

### Auth

`NDIF_POSTGRES_URL` is commented out in the compose file's `api` service and
unset in the published image and a from-source install. With it empty,
`PostgresProvider.enabled()` is False, `verify_api_key` returns `None`, and
`validate_request` defaults **`request.trusted` to true** for any request that
doesn't set it (`src/ndif/services/api/auth.py` `validate_request`).

`trusted` is the most consequential flag in the system:

- A trusted request's traced block runs **in-process inside the model actor**,
  in the same process as the weights — no runner subprocess, no socket. Whatever
  Python the caller submitted executes with the actor's privileges.
- The same flag becomes **`trust_remote_code=`** when a model is sized and
  loaded (`src/ndif/services/ray/deployments/controller/cluster/cluster.py`
  `Cluster.deploy`), so a request naming an arbitrary Hugging Face repo can run
  that repo's code on your GPU node.

So an unauthenticated NDIF is not "an NDIF anyone can use", it is "an NDIF
anyone can run arbitrary code on". Enable auth first, then decide who gets the
`trusted` tag.

Enabling it means: set `NDIF_POSTGRES_URL` on the **api** service (the schema in
`docker/postgres/init.sql` already matches what `verify_api_key` queries), point
it at a Postgres you control with a real password rather than `admin`/`admin`,
and create users and keys — API keys are UUIDs, and a key is a row in `keys`
joined to a `users` row. Grant the `trusted` user_tag only to keys you would
hand your own shell to; grant `priority` to keys that should jump the queue.
Full walkthrough in [Enable auth](../runbooks/enable-auth.md).

**Turning auth on changes behavior for every key without the `trusted` tag.**
Such a request runs in the sandbox runner, against a weightless copy of the
model, so it cannot read or edit weights, cannot take gradients (a `.grad`
read fails), and therefore cannot train. Tell your users before you flip
it; see [Sandbox Execution](../concepts/sandbox-execution.md).

### The sandboxed actor class

Auth decides *whether* a request is trusted. The **model actor class** decides
whether that makes any difference. Only the sandbox actors run an untrusted
request in a separate runner process; the base `ModelActor` runs every request
in-process regardless of `trusted`.

The controller picks the class from `NDIF_MODEL_IMPORT_PATH`, falling back to
`NDIF_DEFAULT_MODEL_ACTOR_CLASS`, falling back to the in-process
`ndif.services.ray.deployments.modeling.base.ModelActor`
(`src/ndif/services/ray/deployments/controller/controller.py`
`ControllerDeploymentArgs`). **Only the compose file sets it** (to
`ndif.services.ray.sandbox.model.SandboxModelActor`, on the `ray` service). The
published image and a from-source install run untrusted code in-process even
with auth on, until you set one of:

| Variable | Value | For |
|---|---|---|
| `NDIF_MODEL_IMPORT_PATH` (or `NDIF_DEFAULT_MODEL_ACTOR_CLASS`) | `ndif.services.ray.sandbox.model.SandboxModelActor` | every replica |
| `NDIF_TP_MODEL_ACTOR_CLASS` | `ndif.services.ray.tp.model.SandboxedTPModelActor` | tensor-parallel replicas, if TP is on |

Set them on the `ray` service (the controller reads them). A tensor-parallel
placement replaces the actor class, so `TPModelActor` would run untrusted code
in-process. A per-deployment `actor_class` (`--actor-class`, `models.yaml`, the
dashboard's deploy modal) also overrides the default — don't point one at the
base `ModelActor` on a cluster that takes untrusted traffic.

The sandbox is process separation, not a hardened jail: no namespaces, seccomp,
rlimits, or filesystem isolation, and the runner runs as the same user as the
actor. Size your threat model accordingly.

### Client versions

`NDIF_MIN_NNSIGHT_VERSION` and `NDIF_MIN_PYTHON_VERSION` reject client versions
you can't serve; unset, there is no gating. See
[Configuration](configuration.md#the-client-version-gate).

## 2. Lock down the dashboard

Compose sets `NDIF_DASHBOARD_DEV_MODE: "true"` on the `dashboard` service, which
makes `require_auth` return the configured username without checking a cookie
(`src/ndif/services/dashboard/backend/auth.py` `require_auth`). The dashboard
can deploy, evict and restart models — it is a control plane, not a viewer.

```bash
# Remove NDIF_DASHBOARD_DEV_MODE entirely, then:
python -m ndif.services.dashboard.backend.auth hash '<your password>'
```

Set `NDIF_DASHBOARD_USERNAME`, the resulting bcrypt hash as
`NDIF_DASHBOARD_PASSWORD_HASH`, and a random 32+ byte
`NDIF_DASHBOARD_SESSION_SECRET` — the default is the literal string
`change-me-please-this-is-not-secure` (`Settings` in
`src/ndif/services/dashboard/backend/config.py`), and anyone who knows it can
forge a session cookie. `NDIF_DASHBOARD_SESSION_TTL_DAYS` (7) bounds how long a
stolen cookie is useful. On a headless box, log in with
`POST /api/auth/login` and reuse the cookie
([Admin Dashboard](dashboard.md#scripting-the-dashboard-api)).

Dashboard deploys default to `trusted: true` (an admin action), so they load
with `trust_remote_code`. Give the dashboard a durable `NDIF_DASHBOARD_DATA_DIR`
volume; its schedule and monitor state live there. See
[Admin Dashboard](dashboard.md).

## 3. Real object storage, reachable from two networks

`NDIF_OBJECT_STORE_ACCESS_KEY` / `_SECRET_KEY` default to `minioadmin` on both
the server and the MinIO container. Replace them, and put the bucket somewhere
durable — the dev MinIO writes to the container filesystem and loses everything
on `just down`.

On a cloud host, prefer holding no key at all. Set **both** variables empty and
boto3 falls back to its own credential chain, which on AWS finds the instance or
task role — so the deployment authenticates as an identity the platform rotates,
scoped by a policy that names the bucket. A static key is a second place the
bucket has to be named, and the two can disagree: an environment given another
environment's key is authenticated but not authorized, and fails every upload
with AccessDenied. The keys are used only when both are set, so a half-configured
pair falls back to the chain as well.

> **Gotcha — the single most common misconfiguration.** There are two endpoint
> variables and they are not interchangeable
> (`src/ndif/common/providers/objectstore.py` `ObjectStoreProvider`).
> `NDIF_OBJECT_STORE_URL` is what the *server* uploads through.
> `NDIF_OBJECT_STORE_PUBLIC_URL` is what presigned GET URLs are **signed with** —
> and a presigned URL is an HMAC over the request including the host, so signing
> with an internal hostname produces a link your users cannot resolve, let alone
> fetch. Set `PUBLIC_URL` to the address a client on the public internet will
> hit. Leave `NDIF_OBJECT_STORE_URL` empty only if you want real AWS S3 for
> `NDIF_OBJECT_STORE_REGION`.

Both variables must be set identically on `api` and `ray` — the model actor
uploads and signs, the API serves `/response/{id}` from the same bucket.

Presigned URLs expire after **one hour** (`ObjectStoreProvider.presigned_get`),
which is not env-configurable. Result blobs are never deleted by NDIF; if you
don't want the bucket to grow forever, add a lifecycle rule on the store itself.

## 4. Multi-node Ray

One variable decides a node's role. `src/ndif/services/ray/start.sh` reads
`NDIF_RAY_HEAD_ADDRESS`: empty ⇒ this node runs `ray start --head` and then
launches the NDIF controller; set ⇒ it waits for that `HOST:PORT` to accept TCP
and joins as a worker.

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

The controller runs on the head and only the head: `ControllerActor` is declared
with `resources={"head": 1}`, and `src/ndif/services/ray/resources.py` advertises
`head=10` only when invoked with `--head`. Each node also advertises
`cuda_memory_bytes` and `cpu_memory_bytes`, which the controller reads back for
placement. A node with no GPU is ignored entirely.

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
code on the cluster. See [Ports](../reference/ports.md) and
[Add a GPU node](../runbooks/add-a-gpu-node.md).

Prometheus's single static `ray:8080` target only covers one node. A multi-node
cluster needs Ray's own metrics service discovery instead — configure that in your
own Prometheus.

## 5. Sizing

A few config knobs decide how the cluster uses its hardware. Set them for your
node, not the dev defaults; the detail lives in the runbooks and
[Environment Variables](../reference/env-vars.md).

- **GPU memory is estimated, not measured.** The controller sizes a model from a
  meta-device estimate plus padding, and never reads the card's real free memory.
  Long-context or heavily-batched workloads can pass placement and then OOM.
  `NDIF_DEFAULT_PADDING_FACTOR` / `NDIF_DEFAULT_PADDING_BIAS` set the global
  headroom, and a per-model `padding_factor` raises it for just the models that
  need it. See [Model OOM on deploy](../runbooks/model-oom-on-deploy.md).
- **The WARM cache is CPU RAM.** `NDIF_MODEL_CACHE_PERCENTAGE` (default `0.9`) is
  the fraction of the node's **total system RAM** the controller may use to hold
  evicted models off-GPU — not a GPU setting. Lower it on a node that runs
  anything else.
- **Replicas are the unit of concurrency.** A replica serves one request at a
  time; `NDIF_AUTOSCALING_MAX_REPLICAS` caps how many per model autoscaling adds.
- **Disk.** The `ray` service needs a persistent Hugging Face cache and, for gated
  repos, `HF_TOKEN`. The dev compose already bind-mounts the host HF cache and
  passes `HF_TOKEN` through; a non-compose deployment must arrange both itself.
  `NDIF_RAY_TEMP_DIR` (`/tmp/ray`) needs real space.

## 6. Timeouts and retention

**`NDIF_DEFAULT_EXECUTION_TIMEOUT_SECONDS` is unset by default, meaning no
execution cap at all** (`ControllerDeploymentArgs`): a block runs until it
finishes and holds its replica for the duration. Right for a box only you submit
to, wrong for a shared one — set it, or a per-model `execution_timeout_seconds`,
before other people can submit.

Several other timeouts (`NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS`, `NDIF_API_TIMEOUT`,
`NDIF_POSTGRES_COMMAND_TIMEOUT_S`, the `/status` and `/env` cache timeouts) carry
defaults tuned for a single-host dev stack. Review them against your workload —
each is documented with its default in [Environment Variables](../reference/env-vars.md).

The dev stack declares no named telemetry, Redis, blob, or Postgres volumes.
API keys survive a container recreate — Postgres's anonymous data volume
reattaches — but `just down` (and so `just ta`) removes the container, the next
boot gets a new empty volume, and `init.sql` re-runs: the keys DB is gone, as are
metrics and results. Only the host HF cache mount and the `dashboard_data` volume
survive `just down`. A real deployment brings its own
durable volumes and retention policy for anything it needs to keep. See
[Observability](observability.md).

## What NDIF does not provide

- **No TLS.** Gunicorn binds plain HTTP on `0.0.0.0:8001`
  (`src/ndif/services/api/gunicorn_conf.py` `bind`); there are no certificate
  options anywhere. API keys travel in the `ndif-api-key` header. Terminate TLS
  yourself and never expose 8001 directly.
- **No key on result retrieval.** `GET /response/{id}` requires no API key
  (`src/ndif/services/api/app.py` `get_response`): anyone holding a request id
  can fetch that request's latest response, including its result URL. Request
  ids are the only thing protecting it.
- **No rate limiting or quotas.** The only per-key behaviors implemented are the
  `trusted` and `priority` tags. A single key can submit unlimited requests.
- **No multi-tenancy.** All users share the same model deployments and the same
  GPUs. There is no per-user isolation beyond the trusted/untrusted execution
  fork, and no accounting.
- **No secret management.** Credentials are environment variables. Bring your own
  secret store.
- **No authentication on the backing services.** The dev compose publishes Redis,
  Postgres, MinIO (`minioadmin`), InfluxDB, Loki, Prometheus, Grafana (anonymous
  admin), the Ray dashboard and the Ray client port on every interface. Anyone who
  can reach Redis or the Ray client port can run code on the cluster. Bind them to
  `127.0.0.1` or drop the `ports:` entries before the machine is reachable;
  [Ports](../reference/ports.md) marks which ports belong public.
- **No namespacing in Redis.** Keys such as `queue`, `status` and `ray:connected`
  carry no prefix, so two deployments sharing one Redis take each other's
  requests. Give each deployment its own Redis, or its own logical database.
- **No test CI and no compatibility guarantees.** `.github/workflows/` builds and
  publishes images and the PyPI package; none of them run tests. The test suite
  under `tests/` needs a live stack. There is no stable public API.
- **No horizontal API tier.** `NDIF_API_WORKERS` scales gunicorn workers in one
  process group, and the queue dispatcher is started exactly once by the gunicorn
  master (`gunicorn_conf.py` `on_starting`). Running two API *containers*
  against one Redis would run two dispatchers — not a configuration the code
  anticipates.

## Pre-flight checklist

| # | Check | Where | Done when |
|---|---|---|---|
| 1 | `NDIF_POSTGRES_URL` set on the API, keys created, `trusted` granted deliberately | api | `GET /whoami` with a key returns your email; without one, `/request` 401s |
| 2 | Model actor class is a sandbox actor: `NDIF_MODEL_IMPORT_PATH` (or `NDIF_DEFAULT_MODEL_ACTOR_CLASS`) = `ndif.services.ray.sandbox.model.SandboxModelActor`, and `NDIF_TP_MODEL_ACTOR_CLASS` = `ndif.services.ray.tp.model.SandboxedTPModelActor` if TP is on | ray | `ndif status --json-output` shows a sandbox `actor_class` on every replica |
| 3 | Users told that untagged keys can no longer read weights, take gradients or train | — | — |
| 4 | Postgres password is not `admin`, and the DB is not published | postgres | — |
| 5 | `NDIF_DASHBOARD_DEV_MODE` removed; username, bcrypt hash, random session secret set | dashboard | The dashboard prompts for a login |
| 6 | Object-store credentials replaced; bucket durable | api, ray | — |
| 7 | `NDIF_OBJECT_STORE_PUBLIC_URL` resolves and fetches **from a client machine** | api, ray | A remote trace's result downloads from off-host |
| 8 | `NDIF_REDIS_URL` explicitly set on **every** service — never left at localhost | all | `ndif info` in each container shows the right host |
| 9 | Head/worker split correct: `NDIF_RAY_HEAD_ADDRESS` set on workers only | ray | `ndif status` lists every GPU node |
| 10 | 6385 / 8076 / 52366 open between nodes only | network | — |
| 11 | Never public: Redis 6379, Postgres 5432, Ray client 10001, Ray dashboard 8265, MinIO console 9001, dashboard 8081 without TLS, the telemetry ports | network | A scan from outside shows only your TLS endpoint (and the object store, if public) |
| 12 | TLS terminating proxy in front of 8001; 8001 not directly exposed | network | `https://` endpoint answers `/ping` |
| 13 | Persistent volumes for HF cache, MinIO, Postgres, Loki, Influx, dashboard data | all | Survives a container recreate |
| 14 | `HF_TOKEN` set on `ray` if you serve gated checkpoints | ray | The gated model deploys |
| 15 | `NDIF_ENVIRONMENT` set to something other than `dev` | all | Grafana can separate this deployment |
| 16 | `NDIF_MODEL_CACHE_PERCENTAGE` sized for what else runs on the node | ray | — |
| 17 | `NDIF_AUTOSCALING_MAX_REPLICAS`, padding factors and an execution timeout reviewed | ray | — |
| 18 | `NDIF_MIN_NNSIGHT_VERSION` set to the oldest client you will support | api | An older client gets a clear 400 |
| 19 | Durable volumes and a retention policy for anything you must keep | telemetry, store | Survives a recreate; disk doesn't fill unbounded |

## Related

- [Enable auth](../runbooks/enable-auth.md) — the auth switch, end to end.
- [Add a GPU node](../runbooks/add-a-gpu-node.md) — joining a worker to an existing head.
- [Configuration](configuration.md) — how these variables layer and where they're read.
- [Environment Variables](../reference/env-vars.md) — every variable and its default.
- [Ports](../reference/ports.md) — the full port map, published and internal.
- [Auth and Limits](../concepts/auth-and-limits.md) — what an API key actually carries.
- [Sandbox Execution](../concepts/sandbox-execution.md) — what the runner separates, and what it does not.
