---
title: Troubleshooting
one_liner: The single "something broke" entry point — a route table to the runbooks and error lookup, the first commands to run, and a symptom → diagnosis → fix table for the stack as a whole.
tags: [operating, runbook, gotchas, api, ray, redis, dashboard, telemetry]
related: [docs/operating/compose-stack.md, docs/operating/configuration.md, docs/operating/cli.md, docs/operating/dashboard.md, docs/operating/observability.md, docs/operating/production.md, docs/concepts/services-and-topology.md, docs/reference/error-messages.md, docs/runbooks/debug-a-stuck-request.md, docs/runbooks/model-oom-on-deploy.md, docs/runbooks/add-a-gpu-node.md, docs/reference/ports.md, docs/reference/env-vars.md]
sources: [docker/docker-compose.yml, docker/Dockerfile, justfile, src/ndif/services/api/app.py, src/ndif/services/ray/queue/dispatcher.py, src/ndif/services/ray/queue/replica.py, src/ndif/services/ray/start.sh, src/ndif/services/ray/resources.py, src/ndif/services/dashboard/backend/app.py, src/ndif/common/providers/objectstore.py, src/ndif/common/providers/loki.py, src/ndif/common/providers/influx.py, src/ndif/cli/commands/doctor.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py]
---

# Troubleshooting

## Where to go

The single entry point for "something broke". Find the row, follow it.

| What you have | Go to |
|---|---|
| Exact error text from a user, or a log line | [Error messages](../reference/error-messages.md) |
| One request hung, or stuck in a status | [Debug a stuck request](../runbooks/debug-a-stuck-request.md) |
| A past failure to reconstruct from a request id and a time | [Trace a user's failed job](../runbooks/trace-a-users-failed-job.md) |
| A deploy refused, or an actor dying while loading weights | [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) |
| A new GPU node that doesn't show up | [Add a GPU node](../runbooks/add-a-gpu-node.md) |
| A killed job's stored response later shows `COMPLETED` | [Trace a user's failed job → Killed jobs](../runbooks/trace-a-users-failed-job.md#killed-jobs) |
| Requests fail right after a kill, with a greenlet `ValueError` (`Cannot switch to a different thread`) | [Debug a stuck request → step 8](../runbooks/debug-a-stuck-request.md#step-8--unstick-it): restart the replica |
| An actor dead for good: `UNHEALTHY` persists and requests to it get an error | [Debug a stuck request → step 4](../runbooks/debug-a-stuck-request.md#step-4--is-there-a-live-replica), the `HOT \| UNHEALTHY` row (not the load-OOM case below) |
| The stack itself: a service down, everything hanging, no UI, no telemetry | the rest of this page |

## The first commands

```bash
just ps                # container status + health, for the whole compose stack
just logs api          # follow one service (api | ray | dashboard | redis | rustfs | ...)
docker compose -f docker/docker-compose.yml exec ray ndif status   # what the controller thinks is deployed
curl -s localhost:8001/ping        # "pong" — the API process is alive, no dependencies
curl -i localhost:8001/connected   # 200 = the dispatcher is running on Ray, 503 = it is not
```

`just logs` follows until Ctrl-C, which detaches without stopping anything.
`ndif status` and `ndif queue` talk to the cluster, not to docker: run them
inside a container as above, or from the host against `NDIF_RAY_ADDRESS`
(`ray://localhost:10001`) and `NDIF_REDIS_URL` with the same Python and Ray
versions as the cluster.

`ndif doctor` checks the *host*: Python, the `ray`, `redis-server` and `rustfs`
binaries on this PATH, a GPU visible to both `nvidia-smi` and torch, and
reachability. Its Binaries section fails by design on a machine that only runs
compose, and a missing `nvidia-smi` is a hard failure. torch, transformers and
ray versions and the Connectivity section are informational and never set the
exit code. Reach for it when a *host* install misbehaves, and for `just ps` when
compose does; [The ndif CLI](cli.md#ndif-doctor) covers getting a `rustfs`
binary. `ndif version` (or `docker run --rm ndif/ndif version`) answers the
narrower "what is installed".

## Symptom → diagnosis → fix

| Symptom | Likely cause | Confirm | Fix |
|---|---|---|---|
| `just up` returns but a container is `Restarting` | Crash at startup | `just logs <svc>` | See [service won't start](#a-service-wont-start) |
| `api` never starts, no logs | It waits on `redis`, `rustfs`, `postgres` and `influxdb` health checks | `just ps` — are they `healthy`? | Fix the unhealthy one, or remove its `depends_on` entry |
| `ray` container fails to create | No NVIDIA container toolkit on the host | `docker info \| grep -i nvidia` | Install the toolkit; see [no GPU](#the-ray-container-has-no-gpu) |
| `ndif status` shows 0 nodes, deploys fail `No GPU nodes available.` | Ray started without a `GPU` resource | `just logs ray` — the `Starting Ray head node with resources:` line | [no GPU](#the-ray-container-has-no-gpu) |
| `/ping` 200 but requests hang, then 503 | The dispatcher stopped; `/ping` does not depend on it, and `ray:connected` takes up to 30 s to lapse | `redis-cli llen queue` stays above zero; `/connected` turns 503 | [ping works, requests hang](#ping-works-but-requests-hang) |
| `/request` 503 "compute backend is reconnecting" | `ray:connected` is absent — no dispatcher is running its loop: the Ray head is down or still starting, or the dispatcher stopped | `redis-cli get ray:connected` | Check the ray container; [ping works, requests hang](#ping-works-but-requests-hang) |
| Dashboard loads JSON, not a UI | No built frontend where it looks | `GET localhost:8081/` returns a `hint` field | [dashboard has no UI](#the-dashboard-has-no-ui) |
| Jobs `COMPLETED`, clients can't download | Presigned URL signed with an unreachable host | `curl` the URL from the client machine | [presigned URLs](#presigned-urls-are-unreachable-from-the-client) |
| `/connected` stays 503 with Ray up | `NDIF_REDIS_URL` unset inside the ray container, so the dispatcher cannot reach Redis | `docker compose -f docker/docker-compose.yml exec ray env \| grep NDIF_REDIS_URL` | [Service names, not localhost](compose-stack.md#service-names-not-localhost) |
| Ray head won't start, port in use | `NDIF_RAY_HEAD_PORT` set to Redis's 6379 | `ss -ltnp \| grep 6379` | Leave it at the default 6385 |
| Grafana panels empty | Loki/Influx not configured for that service | `just logs api \| grep -i "telemetry enabled"` | [telemetry missing](#telemetry-is-missing) |
| `ndif deploy` sits for a long time | Weight download or a slow load — the CLI waits with no deadline | `ndif status` — actor `DEPLOYING`? | Wait. If it cannot come up you get the actor's error, not a timeout |
| `ndif deploy` errors `CANT_ACCOMMODATE` | No node can fit the padded size | `ndif status --verbose` per-GPU free memory | [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) |
| `ndif deploy` errors with an HF `trust_remote_code` message | The repo ships custom modelling code and the key lacks `trust_remote_code` | the evaluator traceback in the error | `ndif deploy --trust-remote-code <checkpoint>`; see [Models and Deployment](models-and-deployment.md#trust_remote_code-in-the-model-key) |
| `ndif evict gpt2` prints `nothing to evict` | Model-key mismatch (revision and task are part of the key), or the replica is only WARM | `ndif status --json-output` for the exact key | Pass the deployment's `--revision` / `--task` |
| `ndif queue` prints `No response from the dispatcher` | Redis is up, the **dispatcher** on the Ray head is not answering — the head is down or still starting, or the dispatcher stopped. The API has no part in it | `curl -i localhost:8001/connected` | [ping works, requests hang](#ping-works-but-requests-hang) |
| `ndif start` printed ✓ but `/connected` never leaves 503 | A service died after its 2-second startup check | `ndif info` — is `ray` `stopped`? `$NDIF_HOME/logs/ray.log` | Read the log — the first error after `Starting Ray head node` is the cause |
| Code changes have no effect | The source is baked into the image | — | `just ta` (down → build → up) |
| `ndif status` / the dashboard wait ~40 s then fail `Starting Ray client server failed`; the `.err` file it names is empty | The Ray client proxier's forked per-client server died at birth (grpc fork handlers skipped) | `ray_client_server.err` in Ray's session logs: `skipping fork() handlers` before `SpecificServer startup failed` | `start.sh` exports `GRPC_ENABLE_FORK_SUPPORT=0` before `ray start`; make sure nothing overrides it |

## A service won't start

Every NDIF container is the **same image** with a different `NDIF_SERVICE`,
whose default command is `ndif start --foreground`. So a startup crash is almost
always in that service's `start.sh` or its first import.

```bash
just ps                      # STATUS column: Restarting / Exited (1)
docker compose -f docker/docker-compose.yml logs --tail=50 api   # or ray / dashboard
```

| What the log shows | Cause |
|---|---|
| Nothing at all, container never runs | A `depends_on: service_healthy` gate. `api` waits on `postgres` **and** `influxdb` even though it can run without either |
| `ERROR: Cannot write to Ray temp directory` | `NDIF_RAY_TEMP_DIR` isn't writable |
| `Waiting for Ray head at ...` forever, then `not reachable after N attempts` | A worker node with `NDIF_RAY_HEAD_ADDRESS` set can't reach the head. Check the port — see [Add a GPU node](../runbooks/add-a-gpu-node.md) |
| A Python `ImportError` for an extra | The image installs its extras with `--no-deps`; a new dependency needs a `requirements.txt` entry |
| `queue/config.py` raising at import | A non-integer or non-positive `NDIF_QUEUE_*` / `NDIF_AUTOSCALING_*` value. A typo fails the process rather than silently defaulting |

The compose project is named `dev`, so the containers are `dev-api-1`,
`dev-ray-1`, `dev-dashboard-1` if you reach past `just`.

## The ray container has no GPU

`src/ndif/services/ray/start.sh` runs `python -m ndif.services.ray.resources
--head`, which reports `cuda_memory_bytes` from `torch.cuda` as a custom Ray
resource. With no visible GPU, Ray advertises no `GPU` resource at all, and
`Cluster.update_nodes` skips every node without one
(`src/ndif/services/ray/deployments/controller/cluster/cluster.py`). The cluster
then has zero nodes, and every deploy fails with `No GPU nodes available.` — a
different failure from `CANT_ACCOMMODATE`, which means nodes exist but none has
room.

```bash
nvidia-smi                                             # on the host
docker compose -f docker/docker-compose.yml exec ray nvidia-smi
docker compose -f docker/docker-compose.yml logs ray | grep "resources:"
```

The `ray` service reserves GPUs with a `deploy.resources.reservations.devices`
block — the compose equivalent of `docker run --gpus all` — which requires the
**NVIDIA container toolkit** on the host. Without it the container fails to
create outright. `nvidia-smi` working inside the container but
`cuda_memory_bytes: 0` means torch sees no GPU: a torch wheel built for a newer
CUDA line than the driver supports, or a `CUDA_VISIBLE_DEVICES` that names no
device in the container. `ndif doctor` says which.

`shm_size: "16gb"` on the `ray` service is not optional either: Ray's plasma
store lives in `/dev/shm`, and Docker's 64 MB default makes Ray spill to disk or
fail outright.

## `/ping` works but requests hang

`/ping` reports the API healthy with no dispatcher running, which defeats naive
monitoring. The signal that follows the dispatcher is the `ray:connected` Redis
key: its dispatch loop renews the key on every pass, with an expiry of three
times `NDIF_QUEUE_FETCH_TIMEOUT_S` (30 s by default), so the key lapses once the
loop stops or the dispatcher dies with the Ray head
(`src/ndif/services/ray/queue/dispatcher.py` `Dispatcher.beat`):

| Endpoint | With no dispatch loop running |
|---|---|
| `GET /ping` | Still passes: no dependencies at all — a live gunicorn worker is enough |
| `GET /connected` | 503 once `ray:connected` has lapsed |
| `POST /request` | Same key. Until it lapses the request is accepted and `LPUSH`ed, then not popped |
| `GET /status`, `GET /env` | Same key |

Requests accepted before the key lapsed wait in the Redis list and their clients
sit on `RECEIVED`. Monitor `/connected`, not `/ping`; the *queue* shows the same
thing from the other side:

```bash
docker compose -f docker/docker-compose.yml exec redis redis-cli llen queue   # 0 or small
docker compose -f docker/docker-compose.yml exec api ndif queue               # 5 s timeout
```

An `llen queue` that stays above zero plus `No response from the dispatcher` is
the signature.
The dispatcher is a detached Ray actor on the head node, launched by
`src/ndif/services/ray/start.sh` after the controller, so restarting the API
does not bring it back. Ray restarts the actor when its process dies, and a new
head starts a new one. To replace one that is alive but stuck, kill the actor:
step 2 of [Debug a Stuck Request](../runbooks/debug-a-stuck-request.md) has the
command.

A dispatcher that goes down **drops every request it had popped.** Redis's
`queue` list is the only durable point in the path; once the dispatcher pops a
request it exists only in that process's memory, and nothing tells its user.
Only requests still in the Redis list survive, and wait for the next
dispatcher. See [Queue Internals](../developing/queue-internals.md).

If the dispatcher is alive but the queue still isn't draining, the request is
stuck further down the path — go to
[Debug a stuck request](../runbooks/debug-a-stuck-request.md).

## The dashboard has no UI

`http://localhost:8081/` answers with JSON instead of the admin app:

```json
{"ok": true, "hint": "Frontend dist not found. Either run `npm run build` in
 src/ndif/services/dashboard/frontend, or use `npm run dev` ...",
 "frontend_dist": "..."}
```

The backend's `create_app` (`src/ndif/services/dashboard/backend/app.py`)
serves the built Vue app only if `frontend_dist` contains an `index.html`;
otherwise it registers that hint route. The build is **committed** and packaged,
so a fresh clone, the image and a `pip install` all carry it. Seeing the hint
means `NDIF_DASHBOARD_FRONTEND_DIST` points somewhere else, `dist/` was deleted
from the checkout, or an `npm run build` failed halfway. Rebuild it:

```bash
cd src/ndif/services/dashboard/frontend
npm ci && npm run build            # writes dist/
cd - && just ta dashboard          # rebuild the image so dist/ is copied in; then `just up`
```

| Neighbouring symptom | Cause |
|---|---|
| The UI opens with no login prompt | `NDIF_DASHBOARD_DEV_MODE: "true"` on the compose `dashboard` service — see [Admin Dashboard](dashboard.md#auth) |
| The reconcile/monitor crons never run | `start.sh` wires cron only when `cron` is on PATH and `/etc/cron.d` is writable — true in the container, false outside it |

## Presigned URLs are unreachable from the client

Jobs reach `COMPLETED` and then the client fails to download. A presigned URL is
an HMAC **over the request including the host**, so it has to be signed with the
address the downloader will actually hit — which is not the address the server
uploads through (`src/ndif/common/providers/objectstore.py`
`ObjectStoreProvider`).

| Variable | Compose value | Role |
|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://rustfs:9000` | the server's own client: uploads and bucket ops |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | `http://localhost:9000` | signs the client's GET |

Both are set on `api` and `ray`. Swap them and every job completes and then
fails to download; leave the public one empty and it falls back to the first,
which is correct for a single-host install and wrong for compose. To reproduce,
take the `data` field of a completed job's `GET /response/{id}` and `curl -I` it
*from the client machine* — exactly what the user's download does.

In production the public URL is whatever your users reach, and it must be
reachable from outside your network. URLs expire after one hour, which matters
for non-blocking jobs polled much later.

## Telemetry is missing

Both telemetry providers are **fail-open** and off until given a URL, so "no
data" is the only symptom.

| Check | Meaning |
|---|---|
| `just logs api \| grep "Loki telemetry enabled"` | Absent: `NDIF_LOKI_URL` unset, so logs go to the console only. Compose sets it for `api` and `ray`, **not** for `dashboard` |
| `just logs api \| grep "InfluxDB telemetry enabled"` | Absent: `NDIF_INFLUX_URL` unset, `NDIF_INFLUX_ENABLED=false`, or the client library missing |
| `NDIF_LOKI_URL is set but python-logging-loki is not installed` | The `metrics` extra wasn't installed |
| Prometheus target down | It scrapes exactly one target, `ray:8080` — Ray's `--metrics-export-port`, from `NDIF_RAY_METRICS_PORT` |

Model-actor logs are the ones people can't find: they are under
`service="model"`, not `service="ray"`. Details in
[Observability](observability.md).

## A model won't deploy

| Error | Meaning | Next |
|---|---|---|
| `No GPU nodes available.` | The cluster has zero GPU nodes | [ray has no GPU](#the-ray-container-has-no-gpu) |
| `CANT_ACCOMMODATE: placed N of M new replicas before the cluster ran out of room.` | Nodes exist; none can fit the padded size, even after every legal eviction | [Model OOM on deploy](../runbooks/model-oom-on-deploy.md) |
| A node-stated reason (for example a `gpus` count the model can't split into) | The request itself can't be placed as asked | Fix the `gpus` / `max_tp` you passed |
| An evaluator traceback (gated repo, 401, unknown repo id) | Sizing failed before placement. `HF_TOKEN` must be present wherever the deploy runs | Set `HF_TOKEN` |
| An HF "requires you to execute the code" error | The repo ships custom modeling code and the key lacks `trust_remote_code` | Deploy with `--trust-remote-code` ([the key field](models-and-deployment.md#trust_remote_code-in-the-model-key)) |
| The `Could not deploy this model` refusal for `trust_remote_code` on a client's request | Nothing is running for that key and no queued request's API key has the tag ([Error Messages](../reference/error-messages.md#error-responses)) | Deploy it as an admin, or grant the key the `trust_remote_code` user_tag |
| `ndif status` shows the actor `UNHEALTHY`, repeatedly | The actor dies during weight load and Ray restarts it | The actor's log in the Ray dashboard; usually a load-time OOM |
| Deploy succeeds, requests still see `DEPLOYING` forever | `Replica.wait` polls `__ray_ready__` with **no timeout** (`src/ndif/services/ray/queue/replica.py`) | `ndif queue` — a Processor at `deploying` with rising depth |

Raising `NDIF_MODEL_CACHE_PERCENTAGE` does **not** help a GPU shortage: it sizes
the **CPU** RAM used as WARM cache. For GPU pressure: evict explicitly, lower
`NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default 3600, which protects young
deployments from automatic eviction), deploy `--pinned` (which waives the age
check for the incoming model), or tune the padding.

## Related

- [The Compose Stack](compose-stack.md) — every container, port, volume, and the
  non-obvious compose decisions.
- [Configuration](configuration.md) and [Environment Variables](../reference/env-vars.md) —
  what each `NDIF_*` knob actually changes.
- [Error messages](../reference/error-messages.md) — every error text, both sides.
- [Observability](observability.md) — the log labels and metric measurements the
  checks above assume.
