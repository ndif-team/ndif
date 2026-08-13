---
title: Troubleshooting
one_liner: Operator triage — the four commands to run first, then a symptom → diagnosis → fix table for the whole stack rather than one request.
tags: [operating, runbook, gotchas, api, ray, redis, dashboard, telemetry]
related: [docs/operating/compose-stack.md, docs/operating/configuration.md, docs/operating/cli.md, docs/operating/dashboard.md, docs/operating/observability.md, docs/operating/production.md, docs/concepts/services-and-topology.md, docs/errors/client-side-failures.md, docs/errors/server-exceptions.md, docs/runbooks/debug-a-stuck-request.md, docs/runbooks/model-oom-on-deploy.md, docs/runbooks/add-a-gpu-node.md, docs/reference/ports.md, docs/reference/env-vars.md, docs/gotchas/networking-and-compose.md, docs/gotchas/gpu-and-memory.md]
sources: [docker/docker-compose.yml, docker/Dockerfile, justfile, src/ndif/services/api/app.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/ray/start.sh, src/ndif/services/ray/resources.py, src/ndif/services/dashboard/backend/app.py, src/ndif/common/providers/objectstore.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/influx.py, src/ndif/cli/commands/doctor.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py]
---

# Troubleshooting

## What this covers

Triage, not procedure. Something is wrong with the *stack* — a service won't
start, requests hang, the dashboard is blank — and you need to know which
component to blame in under a minute. Anything that needs a full walk-through
links to a runbook; anything about one user's request links to
[Client-side failures](../errors/client-side-failures.md).

## The first four commands

```bash
just ps                # container status + health, for the whole compose stack
just logs api          # follow one service (api | ray | dashboard | redis | minio | ...)
ndif status            # what the controller thinks is deployed, and on which GPUs
ndif doctor            # local prerequisites: python, binaries, GPU, reachability
```

`just ps` and `just logs` are thin wrappers over
`docker compose -f docker/docker-compose.yml` (`justfile:14`); `just logs` follows
until Ctrl-C, which detaches without stopping anything. `ndif status` and
`ndif queue` talk to the cluster, not to docker — from the host they use
`NDIF_RAY_ADDRESS` (`ray://localhost:10001`) and `NDIF_REDIS_URL`
(`redis://localhost:6379`), both published by compose, or run them inside a
container with `docker compose exec api ndif status`.

Two caveats on `ndif doctor`: its Binaries section checks for `ray`,
`redis-server` and `minio` **on this host's PATH**, which you don't have if you
only ever run compose, and it treats a missing `nvidia-smi` as a hard failure
even though the non-GPU half of the stack runs fine without one. Connectivity is
informational and never sets the exit code (`_report_connectivity`,
`cli/commands/doctor.py:82`). Reach for it when a *host* install misbehaves;
reach for `just ps` when compose does.

Two more worth having in the muscle memory:

```bash
curl -s localhost:8001/ping        # "pong" — the API process is alive, no dependencies
curl -i localhost:8001/connected   # 200 = Ray reachable, 503 = dispatcher reconnecting
```

## Symptom → diagnosis → fix

| Symptom | Likely cause | Confirm | Fix |
|---|---|---|---|
| `just up` returns but a container is `Restarting` | Crash at startup | `just logs <svc>` | See [service won't start](#a-service-wont-start) |
| `api` never starts, no logs | It waits on `postgres` and `influxdb` health checks | `just ps` — are those two `healthy`? | Fix or remove the `depends_on` entry |
| `ray` container fails to create | No NVIDIA container toolkit on the host | `docker info \| grep -i nvidia` | Install the toolkit; see [no GPU](#the-ray-container-has-no-gpu) |
| `ndif status` shows 0 nodes, deploys fail `No GPU nodes available.` | Ray started without a `GPU` resource | `just logs ray` — the `Starting Ray head node with resources:` line | [no GPU](#the-ray-container-has-no-gpu) |
| `/ping` 200 but every request hangs | The dispatcher died; `ray:connected` has no TTL | `redis-cli llen queue` is growing | [ping works, requests hang](#ping-works-but-requests-hang) |
| `/request` 503 "compute backend is reconnecting" | `ray:connected` is absent — dispatcher looping in `connect()` | `redis-cli get ray:connected` | `just logs api`; check the ray container |
| Dashboard loads JSON, not a UI | `frontend/dist/` was never built | `GET localhost:8081/` returns a `hint` field | [dashboard has no UI](#the-dashboard-has-no-ui) |
| Jobs `COMPLETED`, clients can't download | Presigned URL signed with an unreachable host | `curl` the URL from the client machine | [presigned URLs](#presigned-urls-are-unreachable-from-the-client) |
| Model actors can't publish responses | `NDIF_REDIS_URL` unset inside the ray container | `just logs ray` — connection refused on localhost:6379 | [No Redis on localhost](#no-redis-on-localhost-inside-the-ray-container) |
| Ray head won't start, port in use | `NDIF_RAY_HEAD_PORT` collides with Redis | `ss -ltnp \| grep 6379` | Set `NDIF_RAY_HEAD_PORT=6385` explicitly |
| Grafana panels empty | Loki/Influx not configured for that service | `just logs api \| grep -i "telemetry enabled"` | [telemetry missing](#telemetry-is-missing) |
| `ndif deploy` sits for a long time | Weight download or a slow load — the CLI waits with no deadline | `ndif status` — actor `DEPLOYING`? | Wait. If it cannot come up you get the actor's error, not a timeout |
| `ndif deploy` errors `CANT_ACCOMMODATE` | No node can fit the padded size | `ndif status` per-GPU `available_memory_bytes` | [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) |
| `ndif deploy` errors with an HF `trust_remote_code` message | Every CLI deploy is `trusted=False` | the evaluator traceback in the error | Deploy from the dashboard, which hard-codes `trusted: True` |
| `ndif evict gpt2` prints `nothing to evict` | Model-key mismatch (revision is part of the key), or the replica is only WARM | `ndif status --show-cold` | Name the exact revision, or `--all` |
| `ndif queue` prints `No response from the dispatcher` | Redis is up, the **API** (and its dispatcher) is not | `curl localhost:8001/ping` | Restart `api` |
| Code changes have no effect | The source is baked into the image; there is no bind mount | — | `just ta` (down → build → up) |

## A service won't start

Every NDIF container is the **same image** with a different `NDIF_SERVICE`, whose
entrypoint is `ndif start --foreground` (`docker/Dockerfile:49`). So a startup
crash is almost always in that service's `start.sh` or its first import.

```bash
just ps                      # STATUS column: Restarting / Exited (1)
just logs api                # or ray / dashboard
docker compose -f docker/docker-compose.yml logs --tail=50 api
```

| What the log shows | Cause |
|---|---|
| Nothing at all, container never runs | A `depends_on: service_healthy` gate. `api` waits on `postgres` **and** `influxdb` even though it can run without either |
| `ERROR: Cannot write to Ray temp directory` | `NDIF_RAY_TEMP_DIR` isn't writable (`services/ray/start.sh:22`) |
| `Waiting for Ray head at ...` forever, then `not reachable after N attempts` | A worker node with `NDIF_RAY_HEAD_ADDRESS` set can't reach the head. Check the port — see [Add a GPU node](../runbooks/add-a-gpu-node.md) |
| A Python `ImportError` for an extra | The image installs `[api,ray,metrics,postgres,dashboard]` with `--no-deps`; a new dependency needs a `requirements.txt` entry |
| `queue/config.py` raising at import | A non-integer or non-positive `NDIF_QUEUE_*` / `NDIF_AUTOSCALING_*` value. Deliberate: a typo fails the process rather than silently defaulting |

Remember the compose project is named `dev` (`docker-compose.yml:8`), so the
containers are `dev-api-1`, `dev-ray-1`, `dev-dashboard-1` if you reach past
`just` for `docker logs` or `docker exec`.

## The ray container has no GPU

`services/ray/start.sh` shells out to `python -m ndif.services.ray.resources
--head`, which reports `cuda_memory_bytes` from `torch.cuda` and passes it as a
custom Ray resource. With no visible GPU that number is 0 and Ray advertises no
`GPU` resource at all — and `Cluster.update_nodes` **skips every node without
one** (`cluster/cluster.py:92`). The cluster then has zero nodes, and every
deploy fails with:

```
No GPU nodes available.
```

(`cluster/cluster.py:220`). This is a different failure from `CANT_ACCOMMODATE`,
which means nodes exist but none has room.

Check, in order:

```bash
nvidia-smi                                             # on the host
docker compose -f docker/docker-compose.yml exec ray nvidia-smi
just logs ray | head -20                               # the "resources:" line
ndif status                                            # Nodes: N | Total GPUs: N
```

The compose file reserves GPUs with a `deploy.resources.reservations.devices`
block (`docker-compose.yml:257`-`263`) — the compose equivalent of
`docker run --gpus all` — which requires the **NVIDIA container toolkit** on the
host. Without it the container fails to create outright. Only `ray` gets GPUs;
nothing else in the stack needs one.

> **Gotcha:** `shm_size: "4gb"` (`docker-compose.yml:256`) is not optional either.
> Ray's plasma store lives in `/dev/shm` and Docker's default is 64 MB, which
> makes Ray spill to disk or fail outright.

## `/ping` works but requests hang

`GET /ping` has no dependencies — it answers as long as a gunicorn worker is
alive. That makes it useless for telling you whether work is being *done*.

The trap is `ray:connected`: the dispatcher sets that Redis flag on connect and
deletes it while reconnecting (`queue/dispatcher.py:85`, `:109`), and it has
**no TTL**. If the dispatcher process dies outright the flag survives forever, so
`/connected`, `/request`, `/status` and `/env` all keep reporting healthy while
nothing is dispatched. Requests pile up in the Redis list unserved and clients
sit on `RECEIVED`.

```bash
redis-cli get ray:connected      # "1" — but that proves nothing on its own
redis-cli llen queue             # should be 0 or a small transient number
ndif queue                       # asks the dispatcher directly; 5s timeout
```

A growing `llen queue` plus `No response from the dispatcher` from `ndif queue`
is the signature: the flag says connected, the process is gone. The dispatcher
runs as a child of the API's gunicorn master (`gunicorn_conf.py:61`), so
restarting the API brings it back:

```bash
just restart api
```

> **Gotcha:** that restart **drops every queued and in-flight request.** Redis's
> `queue` list is the only durable point in the path; the moment the dispatcher
> `BRPOP`s a request it exists only as a Python object in that process — in a
> per-model `asyncio.Queue` or in `Replica.current_request`. Clients on a blocking
> websocket get no further status at all, not even an `ERROR`. Only requests still
> sitting in the Redis list survive. See
> [Queue internals](../developing/queue-internals.md).

If the dispatcher is alive but the queue still isn't draining, the request is
stuck further down the path — go to
[Debug a stuck request](../runbooks/debug-a-stuck-request.md).

## The dashboard has no UI

`http://localhost:8081/` answers with JSON instead of the admin app:

```json
{"ok": true, "hint": "Frontend dist not found. Either run `npm run build` in
 src/ndif/services/dashboard/frontend, or use `npm run dev` ...",
 "frontend_dist": "/app/src/ndif/services/dashboard/frontend/dist"}
```

The backend mounts the built Vue app only if `frontend_dist` exists and contains
`index.html` (`dashboard/backend/app.py:57`); otherwise it registers that hint
route instead. And **`dist/` is gitignored and untracked** (`.gitignore:9`), while
`docker/Dockerfile` installs no Node toolchain — it `COPY src/ ./src/` and pip
installs. So a fresh clone plus `just up` produces a dashboard backend whose API
works and whose UI does not exist. It looks like a broken deploy; it is a missing
build step.

```bash
cd src/ndif/services/dashboard/frontend
npm install && npm run build       # writes dist/
cd - && just ta dashboard          # rebuild the image so dist/ is copied in
```

For iterating on the UI, `npm run dev` runs Vite and proxies `/api` to the
backend, so you don't rebuild the image at all. `NDIF_DASHBOARD_FRONTEND_DIST`
overrides the location if you serve a build from elsewhere.

Two neighbouring dashboard symptoms:

| Symptom | Cause |
|---|---|
| The UI opens with no login prompt | `NDIF_DASHBOARD_DEV_MODE: "true"` (`docker-compose.yml:192`) makes `require_auth` return the configured username unchecked |
| The reconcile/monitor crons never run | `start.sh` wires cron only when `cron` is on PATH and `/etc/cron.d` is writable — true in the container, false outside it |

## Presigned URLs are unreachable from the client

Jobs reach `COMPLETED` and then the client fails to download. A presigned URL is
an HMAC **over the request including the host**, so it has to be signed with the
address the downloader will actually hit — which is not the address the server
uploads through.

| Variable | Compose value | Role |
|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://minio:9000` | the server's own client (`objectstore.py:41`) |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | `http://localhost:9000` | signs the client's GET (`objectstore.py:43`, `:93`) |

Both are set on `api` and `ray` (`docker-compose.yml:150`-`151`, `223`-`224`).
Swap them and every job completes and then fails to download; leave the public
one empty and `public_client` falls back to `url`, which is correct for a
single-host install and wrong for compose. To reproduce end to end, take the
`data` field of a completed job's `GET /response/{id}` and `curl -I` it *from the
client machine* — that is exactly what the user's download does.

In production the public URL is whatever your users reach — a load-balanced S3
endpoint or a CDN hostname — and it must be *reachable from outside your
network*. URLs expire after one hour (`objectstore.py:161`), which is a real
constraint for non-blocking jobs polled much later.

## No Redis on localhost inside the ray container

`RedisProvider`'s default is `redis://localhost:6379`, but inside the `ray`
container there is no Redis on localhost. Leave `NDIF_REDIS_URL` unset there and
every model actor tries to reach a Redis that isn't there — the response
handshake fails and nothing a user submits ever gets a status. Compose sets it
explicitly (`docker-compose.yml:213`).

Ray's own GCS is deliberately kept off 6379: `services/ray/start.sh:60` passes
`--port="${NDIF_RAY_HEAD_PORT:-6385}"` and the CLI `DEFAULTS`
(`cli/config.py`) set the same `6385`, so both paths agree and the GCS never
collides with Redis's conventional 6379. If you override `NDIF_RAY_HEAD_PORT`,
override it the same way everywhere the head and its workers read it.

## Telemetry is missing

Both telemetry providers are **fail-open**: nothing errors when they are
unconfigured, so "no data" is the only symptom.

| Check | Meaning |
|---|---|
| `just logs api \| grep "Loki telemetry enabled"` | Loki is opt-in on `NDIF_LOKI_URL` (`providers/loki.py:139`). Unset → logs go to the console only. Compose sets it for `api` and `ray`, **not** for `dashboard` |
| `just logs api \| grep "InfluxDB telemetry enabled"` | Influx is on by default (`NDIF_INFLUX_ENABLED`, `providers/influx.py:70`) but silently no-ops if the client library is missing or the URL is wrong |
| `NDIF_LOKI_URL is set but python-logging-loki is not installed` | The `metrics` extra wasn't installed (`providers/loki.py:187`) |
| Prometheus target down | It scrapes exactly one target, `ray:8080` (`docker/prometheus/prometheus.yml:20`) — Ray's `--metrics-export-port`, set from `NDIF_RAY_METRICS_PORT` (`services/ray/start.sh:66`). NDIF uses no Ray Serve |

Model-actor logs are the ones people can't find: the controller overrides
`NDIF_SERVICE` to `model` in each actor's `runtime_env`, so they are under
`service="model"`, not `service="ray"`. Details in
[Observability](observability.md).

## A model won't deploy

| Error | Meaning | Next |
|---|---|---|
| `No GPU nodes available.` | The cluster model has zero nodes | [ray has no GPU](#the-ray-container-has-no-gpu) |
| `CANT_ACCOMMODATE: placed N of M new replicas before the cluster ran out of room.` | Nodes exist; none can fit the padded size, even after every legal eviction | [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) |
| An evaluator traceback (gated repo, 401, unknown repo id) | Sizing failed before placement. `HF_TOKEN` must be present wherever the deploy runs | Set `HF_TOKEN` |
| An HF "requires you to execute the code" error | The deployment is untrusted; the repo ships custom modeling code | Redeploy trusted: `ndif deploy --trusted`, a `trusted: true` YAML entry, or the dashboard |
| `ndif status` shows the actor `UNHEALTHY`, repeatedly | The actor dies during weight load and Ray restarts it | The actor's log in the Ray dashboard; usually a load-time OOM |
| Deploy succeeds, requests still see `DEPLOYING` forever | `Replica.wait` polls `__ray_ready__` with **no timeout** (`queue/replica.py:130`) | `ndif queue` — a Processor at `deploying` with rising depth |

> **Gotcha:** raising `NDIF_MODEL_CACHE_PERCENTAGE` does **not** help a GPU
> shortage. It scales the fraction of a node's **CPU** RAM usable as WARM cache
> (`controller.py:547`), not GPU memory. For GPU pressure: evict explicitly, lower
> `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default 3600, which protects young
> deployments from automatic eviction), deploy `--pinned` (which waives the age
> check for the incoming model), or tune `NDIF_DEFAULT_PADDING_FACTOR` /
> `NDIF_DEFAULT_PADDING_BIAS`.

## The API reports healthy after the dispatcher died

Worth stating on its own because it defeats naive monitoring. All four of these
can report success with no dispatcher running:

| Endpoint | Why it still passes |
|---|---|
| `GET /ping` | No dependencies at all — a live gunicorn worker is enough |
| `GET /connected` | Reads `ray:connected`, which has no TTL |
| `POST /request` | Same flag; the request is accepted and `LPUSH`ed, then never popped |
| `GET /status`, `GET /env` | Serve the last cached blob until its TTL (60s / 300s) expires; only then do they 503/504 |

A health check that actually detects a dead dispatcher has to observe the
*queue*, not the API: `redis-cli llen queue` staying non-zero, or a round-trip
`ndif queue` (which is a Redis-stream request/response to the dispatcher process
itself, with a 5s timeout). `GET /status` returning 504 after its cache expires is
the slowest but most automatic signal.

## Related

- [The Compose Stack](compose-stack.md) — every container, port, volume, and the
  four non-obvious compose decisions.
- [Configuration](configuration.md) and [Environment variables](../reference/env-vars.md) —
  what each `NDIF_*` knob actually changes.
- [Debug a stuck request](../runbooks/debug-a-stuck-request.md) — when the stack
  is fine and one request isn't.
- [Model OOM on deploy](../runbooks/model-oom-on-deploy.md),
  [Add a GPU node](../runbooks/add-a-gpu-node.md) — the full procedures.
- [Client-side failures](../errors/client-side-failures.md) and
  [Server exceptions](../errors/server-exceptions.md) — error text, both sides.
- [Observability](observability.md) — the log labels and metric measurements the
  checks above assume.
