---
title: Networking and Compose Gotchas
one_liner: Every way NDIF's network layout bites — 6379 meaning two different things inside the ray container, two head-port defaults, two object-store endpoints, localhost provider defaults, what must be open between nodes, and a health flag that keeps saying "connected".
tags: [gotchas, operating, ray, redis, api, cli]
related: [docs/reference/ports.md, docs/reference/env-vars.md, docs/operating/compose-stack.md, docs/operating/production.md, docs/concepts/services-and-topology.md, docs/runbooks/add-a-gpu-node.md, docs/runbooks/debug-a-stuck-request.md, docs/gotchas/gpu-and-memory.md, docs/gotchas/client-server-versions.md]
sources: [docker/docker-compose.yml, src/ndif/services/ray/start.sh, src/ndif/cli/config.py, src/ndif/common/providers/redis.py, src/ndif/common/providers/ray.py, src/ndif/common/providers/objectstore.py, src/ndif/services/api/app.py, src/ndif/services/api/queue/dispatcher.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py]
---

# Networking and Compose Gotchas

## What this covers

The traps that come from *where* things listen rather than what they do. Two
facts produce almost all of them:

1. **Every provider defaults to `localhost`.** `NDIF_REDIS_URL` defaults to
   `redis://localhost:6379` (`src/ndif/common/providers/redis.py:23`),
   `NDIF_RAY_ADDRESS` to `ray://localhost:10001`
   (`src/ndif/common/providers/ray.py:46`), `NDIF_OBJECT_STORE_URL` to
   `http://localhost:9000` (`src/ndif/common/providers/objectstore.py:41`). Those
   defaults are tuned for a single-host `ndif start`. On the compose network they
   are all wrong, and compose overrides them **per service** — miss one and that
   service silently talks to itself.
2. **Ray brings its own port map, and it overlaps Redis.** Ray's GCS default is
   6379, the same port as Redis. Everything below follows from that collision or
   from the two-endpoints-for-one-object-store split.

## `localhost:6379` inside the ray container is not Redis

This is the sharpest edge in the stack. A Ray head started with the script's own
fallback binds its **GCS** on 6379 (`src/ndif/services/ray/start.sh:60`). Inside
that container, a provider that fell back to `redis://localhost:6379` connects to
Ray's control plane and speaks RESP at it.

Compose therefore sets the variable explicitly on the `ray` service:

```yaml
      # The controller and model actors publish responses to redis by service
      # name; without this they'd default to localhost:6379 — which inside this
      # container is Ray's own GCS, not redis (the handshake fails there).
      NDIF_REDIS_URL: redis://redis:6379
```

(`docker/docker-compose.yml:210-213`.)

The symptom when this is missing is not a startup crash. The controller and the
model actors come up, a request runs to completion on the GPU, and then
`request.respond(...)` cannot publish — so the client sits at `DISPATCHED` or
`RUNNING` forever while the job is already done. Model actors inherit their Redis
address from the controller process: `_provider_runtime_env`
(`.../controller/cluster/deployment.py:16`) exports the controller's provider
config into every actor's `runtime_env`, so fixing it on the `ray` service fixes
it everywhere downstream — and getting it wrong there breaks every actor at once.

## Two defaults for the Ray head port

| Where | Value | Wins when |
|---|---|---|
| `src/ndif/cli/config.py:29` (`DEFAULTS`) | `6385` | always, in practice — the CLI is the container entrypoint (`ndif start --foreground`) and layers `DEFAULTS` *beneath* the real environment |
| `src/ndif/services/ray/start.sh:60` | `6379` | only if you run `start.sh` directly, outside the CLI |

The CLI's 6385 is deliberate: the comment above `DEFAULTS` says the service
defaults "either collide (Ray's own GCS port is 6379, same as Redis) or assume
docker service-name hosts". The bare `${NDIF_RAY_HEAD_PORT:-6379}` in the script
is the latent half of the same collision. Consequences:

- **Joining a worker:** `NDIF_RAY_HEAD_ADDRESS` must name the port the head
  actually bound. Through the CLI or the compose image that is `6385`, not 6379.
  See [add-a-gpu-node](../runbooks/add-a-gpu-node.md).
- **Running `start.sh` by hand on a host that already runs Redis:** `ray start`
  fails to bind, or worse, the two services fight over the port. Set
  `NDIF_RAY_HEAD_PORT` explicitly rather than relying on either fallback.

## The object store has two endpoints, and only one of them signs

Results are uploaded by the server and downloaded by the client, from different
networks. `ObjectStoreProvider` keeps two boto3 clients for exactly this
(`src/ndif/common/providers/objectstore.py:39-49`):

| Variable | Default | Used for |
|---|---|---|
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | the server-side client: bucket creation and `PUT`. Leave empty for real AWS S3 — boto3 derives the endpoint from `NDIF_OBJECT_STORE_REGION`. |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | `""` → falls back to `NDIF_OBJECT_STORE_URL` | the presigning client. **Only** used to sign GET URLs. |

A presigned URL is an HMAC computed over the whole request **including the
host**. Sign with a host the downloader cannot reach and the job completes
normally, the `COMPLETED` response carries a URL, and the client fails at
download — a failure that arrives after all the expensive work is done. Sign with
the right host but the wrong scheme or port and the signature itself is invalid,
which surfaces as a 403 from the object store rather than a connection error.

In compose both are set on `api` and `ray` (`docker-compose.yml:150-151`,
`223-224`): upload through `http://minio:9000`, sign for `http://localhost:9000`
because the client is on the host. The moment the client is *not* on the host —
a colleague on your LAN, a notebook on another machine — `localhost:9000` is
their machine, and every download 404s or hangs.

> **Rule:** `NDIF_OBJECT_STORE_PUBLIC_URL` must be the address **the nnsight
> client** resolves, not the address the server uses. It is the one URL in the
> stack that is about someone else's network.

`NDIF_OBJECT_STORE_REGION` is set explicitly (default `us-east-1`) so presigning
never round-trips to the public endpoint to discover a region — the server
usually can't reach that endpoint at all.

## Service name versus localhost, service by service

| Variable | Provider default | What compose sets | On which services |
|---|---|---|---|
| `NDIF_REDIS_URL` | `redis://localhost:6379` | `redis://redis:6379` | `api` (`:140`), `ray` (`:213`), `dashboard` (`:186`) |
| `NDIF_RAY_ADDRESS` | `ray://localhost:10001` | `ray://ray:10001` | `api` (`:141`), `dashboard` (`:185`) — **not** `ray` itself, which starts the cluster rather than dialing it |
| `NDIF_OBJECT_STORE_URL` | `http://localhost:9000` | `http://minio:9000` | `api`, `ray` |
| `NDIF_OBJECT_STORE_PUBLIC_URL` | (falls back to the above) | `http://localhost:9000` | `api`, `ray` |
| `NDIF_LOKI_URL` | unset (shipping off) | `http://loki:3100/loki/api/v1/push` | `api`, `ray` — **not** `dashboard`, whose logs stay on stdout |
| `NDIF_INFLUX_URL` | unset (metrics off) | `http://influxdb:8086` | `api`, `ray` |
| `NDIF_API_URL` | — | `http://api:8001` | `dashboard` (`:183`) |
| `NDIF_POSTGRES_URL` | `""` (auth off) | **commented out** (`:156`) | — |

Two asymmetries worth remembering: the `ray` service gets no `NDIF_RAY_ADDRESS`
(it *is* the cluster), and the `dashboard` gets no Loki or Influx (its telemetry
is local files in `dashboard_data`).

Reproducing a compose behavior outside compose means reproducing this table.
`NDIF_DEFAULT_MODEL_ACTOR_CLASS` is the other one people miss — compose sets the
sandbox actor (`:228`) while the code default is the in-process one, so a bare
`pip install` + `ndif start` runs a different execution path. See
[Compose stack](../operating/compose-stack.md).

## What has to be reachable between nodes

Only the head binds the cluster-control ports; a worker dials *out*
(`start.sh:38` probes the head with `/dev/tcp` before `ray start --address`, and
never listens for NDIF traffic). For a multi-node cluster, open **between nodes
only**:

| Port | Env var | Why |
|---|---|---|
| 6385 | `NDIF_RAY_HEAD_PORT` | GCS — how a worker joins |
| 8076 | `NDIF_RAY_OBJECT_MANAGER_PORT` | plasma object transfer between nodes |
| Ray's raylet / node-manager / worker port range | none — `start.sh` passes no flag | Ray defaults; consult Ray's port documentation, this repo does not pin them |

Ports 10001 (Ray client) and 8265 (Ray dashboard) are for *services*, not for
worker joins. The client port is the one to be careful with: `ray://` has no
authentication, so anything that can route to 10001 can run arbitrary code on
the cluster. The dev compose publishes both to the host
(`docker-compose.yml:252-253`).

Every node also needs to reach Redis, the object store, and (if configured) Loki
and InfluxDB — model actors publish responses and upload results themselves,
directly, not through the head.

## `ray:connected` has no TTL

The dispatcher — a separate process spawned by the API's gunicorn master — owns
this flag. It `delete`s it on entering `connect()`
(`src/ndif/services/api/queue/dispatcher.py:85`) and `set`s it to `"1"` once Ray
and the Controller actor answer (`:109`). There is **no expiry on the `set`**.

Four API routes depend on it — `POST /request`, `GET /status`, `GET /env`, and
`GET|HEAD /connected` — all through the `require_ray_connection` dependency,
which 503s when the key is absent (`src/ndif/services/api/app.py:106-119`).

So the flag means "the dispatcher successfully connected at some point and has
not since re-entered `connect()`". If the dispatcher process **dies** — killed,
OOMed, crashed outside the reconnect path — the key stays set. `/connected`
keeps answering `{"status": "connected"}`, `POST /request` keeps accepting
requests, and those requests pile up in the Redis queue with nothing popping
them. The user sees `RECEIVED` and then silence.

`/ping` is unaffected (`app.py:321`): it only proves the API process is alive,
which it is.

```bash
# The flag says yes...
curl -sf localhost:8001/connected
# ...but is anything draining the queue?
redis-cli -u "$NDIF_REDIS_URL" llen queue
docker compose -f docker/docker-compose.yml exec api ps aux | grep dispatcher
```

A growing `queue` length plus a set `ray:connected` is the signature. Restarting
the `api` service respawns the dispatcher, which deletes and re-sets the flag
honestly. [Debug a stuck request](../runbooks/debug-a-stuck-request.md) walks the
rest of the diagnosis.

## The dev compose publishes almost everything

`docker/docker-compose.yml` is a single-machine development stack, and it binds
to the host: Redis 6379 (`:13-14`), Postgres 5432 (`:108-109`), Grafana 3000
(`:86-87`) with **anonymous admin and the login form disabled** (`:74-77`),
MinIO 9000/9001 with `minioadmin`/`minioadmin` (`:120-125`), InfluxDB 8086 with a
hardcoded token, Loki 3100, Prometheus 9090, the Ray dashboard 8265 and the Ray
client port 10001.

None of that is authenticated and none of it is behind TLS. On a laptop that is
fine. On any machine with a routable address it is a full compromise: Redis
alone lets an attacker inject pickled requests into `queue`, and 10001 lets them
run code on the GPUs. Bind these to `127.0.0.1` or drop the `ports:` entries
before the machine is reachable — [Production](../operating/production.md) covers
the full change list, and [Ports](../reference/ports.md) marks each port
public-or-not.

> **Related trap: Redis keys are unprefixed.** `queue`, `status`, `env` and
> `ray:connected` carry no namespace, so two NDIF deployments sharing one Redis
> instance will steal each other's requests. Give each its own Redis (or its own
> logical database).

## Quick checks

```bash
# What did each service actually resolve its providers to?
docker compose -f docker/docker-compose.yml exec ray env | grep '^NDIF_' | sort

# Is Redis reachable from the ray container by service name (not localhost)?
docker compose -f docker/docker-compose.yml exec ray \
  python -c "import redis,os; print(redis.from_url(os.environ['NDIF_REDIS_URL']).ping())"

# What host is being signed into result URLs?
docker compose -f docker/docker-compose.yml exec ray \
  sh -c 'echo "$NDIF_OBJECT_STORE_URL -> $NDIF_OBJECT_STORE_PUBLIC_URL"'
```

`ndif doctor` and `ndif info` report the same resolved values from the CLI's
point of view; see [CLI](../operating/cli.md).

## Related

- [Ports](../reference/ports.md) — the full port table, with which env var moves
  each one and whether it belongs on a public interface.
- [Env vars](../reference/env-vars.md) — every `NDIF_*` variable, its default,
  and the line that reads it.
- [Compose stack](../operating/compose-stack.md) — the compose file service by
  service.
- [Services and topology](../concepts/services-and-topology.md) — what talks to
  what, and what degrades when a piece is missing.
- [GPU and memory gotchas](gpu-and-memory.md) — the other half of the compose
  file: `shm_size`, the GPU reservation, and the memory ledger.
- [Add a GPU node](../runbooks/add-a-gpu-node.md) — the worker join, which
  depends on getting the head port right.
