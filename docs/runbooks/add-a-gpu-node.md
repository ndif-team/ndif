---
title: Add a GPU Node
one_liner: Join a second GPU machine to a running NDIF cluster as a Ray worker, confirm the controller can see its GPUs, and drain it again.
tags: [runbook, operating, ray, controller]
related: [docs/operating/production.md, docs/developing/ray-service.md, docs/developing/controller-internals.md, docs/reference/ports.md, docs/reference/env-vars.md, docs/concepts/deployments-and-eviction.md, docs/gotchas/networking-and-compose.md, docs/runbooks/deploy-and-pin-a-model.md]
sources: [src/ndif/services/ray/start.sh, src/ndif/services/ray/resources.py, src/ndif/cli/commands/start.py, src/ndif/cli/service.py, src/ndif/cli/config.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/controller.py, docker/docker-compose.yml]
---

# Add a GPU Node

## What this covers

Taking a cluster that runs on one machine and adding a second GPU box to it,
end to end: what the new node needs installed, which ports have to be open
between the two, how to start it as a worker instead of a head, how to prove the
controller sees its GPUs, and how to take it back out without killing user jobs.

Three facts decide everything below.

1. **Head vs worker is decided by one variable.** `NDIF_RAY_HEAD_ADDRESS` unset
   → `ray/start.sh` runs `ray start --head` and launches the NDIF controller.
   Set to the head's `HOST:PORT` → the same script runs `ray start --address`
   and nothing else (`src/ndif/services/ray/start.sh:50`, `:71-88`). This is
   deliberately separate from `NDIF_RAY_ADDRESS`, which is only the `ray://`
   *client* address the API and CLI dial.
2. **A worker runs Ray and nothing else.** No API, no Redis, no MinIO, no
   controller. `ndif start` with `NDIF_RAY_HEAD_ADDRESS` set defaults its target
   list to `[ray]` alone (`src/ndif/cli/commands/start.py:134`), and the
   controller actor is pinned to the head by the `head` custom resource
   (`controller.py:527`: `@ray.remote(..., resources={"head": 1})`, and
   `resources.py:41` only emits `head=10` under `--head`).
3. **The controller only manages nodes that report GPUs.** `Cluster.update_nodes`
   skips any Ray node without a `GPU` resource (`cluster/cluster.py:92`). A
   CPU-only box can join Ray, but no model will ever be placed on it.

## Before you start

On the new machine:

| Requirement | Why | Check |
|---|---|---|
| NVIDIA driver + `nvidia-smi` | `resources.py` sums `torch.cuda.mem_get_info` per device to advertise `cuda_memory_bytes`; no CUDA → no GPU resource → the controller ignores the node | `nvidia-smi` |
| NVIDIA container toolkit (containers only) | the `ray` service declares `driver: nvidia, count: all` (`docker-compose.yml:257-263`) | `docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi` |
| `/dev/shm` ≥ a few GB | Ray's plasma store lives in `/dev/shm`; Docker's 64 MB default is far too small. The compose `ray` service sets `shm_size: "4gb"` (`docker-compose.yml:256`) | `df -h /dev/shm` |
| The same NDIF + nnsight + torch versions as the head | model actors are constructed from a `model_key` and unpickled requests; a version split between nodes surfaces as deserialization errors on whichever node happens to get the replica | `ndif env` (cluster) vs `ndif env --local` |
| A HuggingFace cache, and `HF_TOKEN` for gated repos | the actor downloads weights on the node it lands on. The controller propagates its *provider* env into each actor's `runtime_env` (`cluster/deployment.py:174-188`) but **not** `HF_TOKEN` / `HF_HOME` — those must exist in the worker's own environment | `ls ~/.cache/huggingface/hub` |

Run `ndif doctor` on the new machine: it checks Python ≥ 3.12, the `ndif` and
`nnsight` packages, the `ray` binary, and the GPU, and exits non-zero if any of
those are missing (`src/ndif/cli/commands/doctor.py:98`). The `redis-server` /
`minio` lines will fail on a worker — that is expected, a worker runs neither.

## Open the ports

From the worker to the head, at minimum:

| Port | Var | What it is |
|---|---|---|
| 6385 | `NDIF_RAY_HEAD_PORT` | Ray GCS — the address you put in `NDIF_RAY_HEAD_ADDRESS` |
| 8076 | `NDIF_RAY_OBJECT_MANAGER_PORT` | Ray object-manager (plasma transfer between nodes) |

Ray also opens node-manager and per-worker ports that `start.sh` never pins, and
those connections go **both** directions. Put both machines on a trusted private
network (or a VPN) and allow traffic between them rather than trying to enumerate
a port list. Full table in [docs/reference/ports.md](../reference/ports.md).

> **Gotcha:** 6385, not 6379. Both `ray/start.sh:60` and the CLI's `DEFAULTS`
> (`src/ndif/cli/config.py`) fall back to `NDIF_RAY_HEAD_PORT=6385`, deliberately
> offset from Redis's 6379 (which is also Ray's *native* GCS default). Use 6385,
> and if you override it, use the same value on the head and every worker.

> **Gotcha:** the dev compose file publishes only `8265` and `10001` from the
> `ray` service. **6385 and 8076 are not published**, so a worker on another host
> cannot reach a head started by a stock `just up`. For a multi-node cluster give
> the head's `ray` service `network_mode: host`, or publish 6385/8076 (and accept
> that Ray's remaining dynamic ports still need a route).

## Start the worker

Bare metal or a VM, with `ndif` installed:

```bash
# on the new GPU machine
NDIF_RAY_HEAD_ADDRESS=10.0.0.5:6385 ndif start ray
```

Equivalently `ndif start --ray-head-address 10.0.0.5:6385` — the flag sets the
same variable (`src/ndif/cli/config.py:38`). Either way `ndif start` detaches the
process and captures its output; follow it with `ndif logs ray -f`.

In a container, using the image compose builds for the `ray` service (`dev-ray`
under the compose project name `dev`; confirm with `docker images`):

```bash
docker run -d --name ndif-worker \
  --gpus all --shm-size=4gb --network host \
  -e NDIF_SERVICE=ray \
  -e NDIF_RAY_HEAD_ADDRESS=10.0.0.5:6385 \
  -e HF_TOKEN="$HF_TOKEN" \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  dev-ray
```

`NDIF_SERVICE=ray` makes the entrypoint start only Ray
(`src/ndif/cli/service.py:85`). `--network host` sidesteps the port-mapping
problem above; without it you must publish Ray's ports and make the container's
advertised address routable from the head.

The worker script blocks until the head's `HOST:PORT` accepts a TCP connection
before joining, retrying `NDIF_RAY_HEAD_WAIT_RETRIES` times (default 60) every
`NDIF_RAY_HEAD_WAIT_INTERVAL_S` seconds (default 2), then exits non-zero
(`start.sh:30-48`). So boot order doesn't matter, but a firewall does — a worker
stuck on `Waiting for Ray head at ...` is a network problem, not a Ray problem.

Expected output on the worker:

```
Waiting for Ray head at 10.0.0.5:6385...
Ray head is reachable.
Starting Ray worker, joining 10.0.0.5:6385, resources: {"cuda_memory_bytes": 171798691840, "cpu_memory_bytes": 540671254528}
```

## Verify the node joined

**Ray sees it.** On the head:

```bash
ray status
```

Two nodes, and the totals should include the new GPUs.

**The controller sees it.** From anywhere that can reach `NDIF_RAY_ADDRESS`:

```bash
ndif status
```

```
NDIF Cluster Status
============================================================

Cluster Resources:
  Nodes: 2
  Total GPUs: 10
  GPU Memory: 632.4 / 796.9 GB free
```

`Nodes` counts entries in the controller's own `Cluster.nodes` dict, which is
rebuilt from `list_nodes(detail=True)` on a loop every
`NDIF_CONTROLLER_SYNC_INTERVAL_S` seconds (default 30, `controller.py:109-114`).
So allow up to ~30s after the worker joins before it appears. `Total GPUs` and
the memory line are derived from each node's `gpu_details`
(`src/ndif/cli/commands/status.py:119-133`) — if `Nodes` went to 2 but the GPU
count didn't move, the new node reported no GPU resource and the controller is
ignoring it (`cluster.py:92`).

**Per-node detail:**

```bash
ndif status --verbose | jq '.cluster.nodes[] | {name, resources: .resources}'
```

`--verbose` returns `controller.get_state()`, whose `cluster.nodes` array carries
each node's `gpu_type`, `total_gpus`, `gpu_memory_bytes`, `cpu_memory_bytes`, and
per-GPU availability (`cluster/node.py:140-172`).

**Prove a model can land there.** Deploy something small enough to fit anywhere
and watch which node it goes to; placement picks randomly among equally good
candidates (`cluster.py:223`), so a node with free GPUs will get its share:

```bash
ndif deploy gpt2 --replicas 2
ndif status --verbose | jq '.cluster.nodes[] | {name, deployments: [.deployments[].model_key]}'
```

The Ray dashboard at `http://<head>:8265` is the cross-check: the Cluster page
lists both raylets, and the Actors page lists the detached actors — the
controller as `Controller` and each replica as
`{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace
(`cluster/deployment.py:105-106`). NDIF does not use Ray Serve; there is no
Serve application to look at.

## Drain and remove a node

There is no manual per-replica dance. **Stop Ray on the worker** — `ndif stop ray`,
or stop the container — and the controller cleans up the cluster view itself.
Within one `NDIF_CONTROLLER_SYNC_INTERVAL_S` tick, `update_nodes` finds the node
missing from `list_nodes`, pops it, and calls `node.purge()`, which drops every
HOT and WARM deployment record it held (`cluster.py:137-141`, `node.py:432-436`).
`ndif status` shows `Nodes` fall back:

```bash
ndif status
```

Nothing is lost silently, even if you pull the node without warning. A dispatch to
an actor on the dead node raises `ActorDiedError`, which the queue classifies as
an eviction (`queue/replica.py:52`): the in-flight request is put back at the
*front* of its model's queue and the replica drops out of the pool
(`replica.py:231-251`), so it is retried on a surviving replica or on a freshly
provisioned one. The user sees the job sit in QUEUED again, not an error. Expect
the controller to keep believing the node exists for up to one
`NDIF_CONTROLLER_SYNC_INTERVAL_S` tick, and an `ndif deploy` in that window may try
to place on the node that just left.

## Gotchas

- **The controller lives on the head, permanently.** It requires `resources={"head": 1}`
  and only the head advertises `head=10`. If you ever run two heads you have two
  clusters, not a bigger one.
- **`cpu_memory_bytes` is total host RAM**, sampled at `ray start`
  (`resources.py:20-24`). The controller then multiplies it by
  `NDIF_MODEL_CACHE_PERCENTAGE` (default 0.9) to get the node's WARM-cache budget
  (`cluster.py:106-109`). A worker with little RAM will hold few WARM models
  regardless of its GPUs.
- **Per-GPU memory is assumed uniform.** `update_nodes` divides the node's total
  `cuda_memory_bytes` by its GPU count and gives every GPU that value
  (`cluster.py:103-114`). A node with mixed card sizes will be mis-accounted.
- **Node capacity is read once, when the node first appears.** `update_nodes`
  only builds a `Node` for ids it hasn't seen (`cluster.py:100`); it never
  re-reads resources for an existing node. To change a node's advertised
  resources, drain it, stop Ray, and rejoin.
- **A worker doesn't need `NDIF_REDIS_URL`, `NDIF_OBJECT_STORE_URL`, or the
  telemetry variables.** The controller exports its own provider configuration
  into every actor's `runtime_env` when it creates one
  (`cluster/deployment.py:174-188`), so actors on a worker connect to the same
  Redis/MinIO/Loki/Influx the head does. Setting them on the worker has no effect
  on the actors.

## Related

- [docs/reference/ports.md](../reference/ports.md) — every port, which variable
  moves it, and what the dev compose publishes.
- [docs/developing/ray-service.md](../developing/ray-service.md) — what
  `start.sh` does line by line, and how `resources.py` feeds the controller.
- [docs/concepts/deployments-and-eviction.md](../concepts/deployments-and-eviction.md)
  — how the controller chooses a node and what HOT/WARM/COLD mean.
- [docs/operating/production.md](../operating/production.md) — the rest of the
  multi-node story: real auth, a shared object store, secrets.
- [docs/runbooks/deploy-and-pin-a-model.md](deploy-and-pin-a-model.md) — put a
  model on the new capacity and keep it there.
