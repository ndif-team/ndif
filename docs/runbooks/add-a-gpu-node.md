---
title: Add a GPU Node
one_liner: Join another GPU machine to a running NDIF cluster as a Ray worker, confirm the controller sees its GPUs, and take it out again without stranding requests.
tags: [runbook, operating, ray, controller]
related: [docs/operating/production.md, docs/developing/ray-service.md, docs/developing/controller-internals.md, docs/reference/ports.md, docs/reference/env-vars.md, docs/concepts/deployments-and-eviction.md, docs/operating/compose-stack.md, docs/operating/troubleshooting.md, docs/runbooks/deploy-and-pin-a-model.md]
sources: [src/ndif/services/ray/start.sh, src/ndif/services/ray/resources.py, src/ndif/cli/commands/start.py, src/ndif/cli/service.py, src/ndif/cli/config.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/cluster/deployment.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/api/queue/replica.py, docker/docker-compose.yml, docker/Dockerfile]
---

# Add a GPU Node

## What this covers

Adding a second GPU machine to a cluster that runs on one: what it needs, which
ports must be open, how to start it as a worker, how to prove the controller sees
its GPUs, and how to take it out again.

Three facts decide everything below:

1. **One variable picks head or worker.** With `NDIF_RAY_HEAD_ADDRESS` unset,
   `src/ndif/services/ray/start.sh` runs `ray start --head` and then the NDIF
   controller. Set to the head's `HOST:PORT`, it runs `ray start --address` and
   nothing else. `NDIF_RAY_ADDRESS` is unrelated: it is the `ray://` client
   address the API and CLI dial.
2. **A worker runs Ray and nothing else** — no API, Redis, MinIO or controller.
   `ndif start` with `NDIF_RAY_HEAD_ADDRESS` set starts only `ray`
   (`src/ndif/cli/commands/start.py` `start`), and the controller actor requires
   the `head` custom resource, which only the head advertises
   (`src/ndif/services/ray/resources.py` `main`).
3. **The controller only manages nodes that report GPUs.** `Cluster.update_nodes`
   skips any Ray node without a `GPU` resource. A CPU-only box can join Ray, but
   nothing is ever placed on it.

## Before you start

On the new machine:

| Requirement | Why | Check |
|---|---|---|
| NVIDIA driver | `resources.py` sums each device's memory into `cuda_memory_bytes`; no CUDA → no GPU resource → the controller ignores the node | `nvidia-smi` |
| NVIDIA container toolkit (containers only) | the worker container needs `--gpus all` | `docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu22.04 nvidia-smi` |
| `/dev/shm` of a few GB | Ray's object store lives there; Docker's 64 MB default is far too small | `df -h /dev/shm` |
| The same NDIF, nnsight, torch and Python as the head | requests and models are unpickled on whichever node holds the replica; a version split surfaces as deserialization errors on that node only | `ndif version` on both; `ndif env` vs `ndif env --local` |
| A HuggingFace cache, and `HF_TOKEN` for gated repos | the actor downloads weights on its own node. The controller passes its Redis, object-store and telemetry settings into each actor, but **not** `HF_TOKEN` / `HF_HOME` — those must be in the worker's own environment | `ls ~/.cache/huggingface/hub` |

`ndif doctor` checks the prerequisites on a host install; on a worker its
`redis-server` and `minio` lines fail, which is expected. Details in
[Troubleshooting](../operating/troubleshooting.md).

## Open the ports

From the worker to the head, at minimum:

| Port | Var | What it is |
|---|---|---|
| 6385 | `NDIF_RAY_HEAD_PORT` | Ray GCS — the port in `NDIF_RAY_HEAD_ADDRESS`. Use the same value on head and workers |
| 8076 | `NDIF_RAY_OBJECT_MANAGER_PORT` | the head's object manager |

Ray also opens node-manager, object-manager and per-worker ports that
`start.sh` doesn't pin (a worker's object-manager port is random), in both
directions. Put the machines on a private network or VPN and allow all traffic
between them rather than enumerating ports. Full table, and why it isn't 6379, in
[Ports](../reference/ports.md).

> **Gotcha:** the dev compose file publishes only `8265` and `10001` from the
> `ray` service, so a worker on another host cannot reach a head started by
> `just up`. For multi-node, run the head's `ray` service with
> `network_mode: host`, or publish 6385/8076 and give Ray's remaining ports a
> route. See [The compose stack](../operating/compose-stack.md).

## Start the worker

Host install, with `ndif` installed:

```bash
# on the new GPU machine
NDIF_RAY_HEAD_ADDRESS=10.0.0.5:6385 ndif start ray
ndif logs ray -f
```

`ndif start --ray-head-address 10.0.0.5:6385` sets the same variable.

In a container, use the same image and tag as the head (for the compose stack,
the image compose built for the `ray` service; `docker images` lists it):

```bash
docker run -d --name ndif-worker \
  --gpus all --shm-size=4gb --network host \
  -e NDIF_SERVICE=ray \
  -e NDIF_RAY_HEAD_ADDRESS=10.0.0.5:6385 \
  -e HF_TOKEN="$HF_TOKEN" \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  <the head's image:tag>
```

`NDIF_SERVICE=ray` is required: the image defaults to `all`, which would start
Redis, MinIO and the API on the worker too (`docker/Dockerfile`). `--network host`
avoids the port-mapping problem above; without it you must publish Ray's ports
and make the container's advertised address routable from the head.

The worker waits for the head's `HOST:PORT` to accept a TCP connection, retrying
`NDIF_RAY_HEAD_WAIT_RETRIES` times (default 60) every
`NDIF_RAY_HEAD_WAIT_INTERVAL_S` seconds (default 2), then exits non-zero. Boot
order doesn't matter; a firewall does — a worker stuck on
`Waiting for Ray head at …` is a network problem.

Expected output:

```
Waiting for Ray head at 10.0.0.5:6385...
Ray head is reachable.
Starting Ray worker, joining 10.0.0.5:6385, resources: {"cuda_memory_bytes": 171798691840, "cpu_memory_bytes": 540671254528}
```

`cuda_memory_bytes: 0` means torch can't see the GPUs; the controller will ignore
this node.

## Verify the node joined

**Ray sees it** — on the head (`docker compose -f docker/docker-compose.yml exec ray ray status`
on the compose stack):

```bash
ray status
```

Two nodes, and the GPU total includes the new cards.

**The controller sees it** (on the compose stack, prefix `ndif` with
`docker compose -f docker/docker-compose.yml exec -T ray`):

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

The controller re-syncs its node list every `NDIF_CONTROLLER_SYNC_INTERVAL_S`
(default 30), so allow that long. If `Nodes` went up but `Total GPUs` didn't, the
node reported no GPU resource and is being ignored.

Per node, from the controller's own state:

```bash
ndif status --verbose | jq '.cluster.nodes[] |
  {name, total_gpus: .resources.total_gpus, gpu_memory_bytes: .resources.gpu_memory_bytes}'
```

**Prove a model can land there.** Placement picks at random among equally good
nodes (`Cluster.deploy`), so a few small replicas spread across them:

```bash
ndif deploy openai-community/gpt2 --replicas 2
ndif status --verbose | jq '.cluster.nodes[] | {name, deployments: [.deployments[].model_key]}'
```

In the Ray dashboard (`http://<head>:8265`) the Cluster page lists both nodes and
the Actors page lists the controller and each replica as
`{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace.

## Take a node out

There is no drain. Stopping Ray on a worker kills the model actors on it; the
controller notices on its next sync and drops the node and every deployment it
held (`src/ndif/services/ray/deployments/controller/cluster/cluster.py`
`Cluster.update_nodes`). A request that was running on one of those actors gets
`ActorDiedError`, which the queue treats as an eviction: the request goes back to
the front of its model's queue and runs again from the start on another replica
(`src/ndif/services/api/queue/replica.py` `Replica.dispatch`). The user sees a
second `QUEUED`, not an error.

Until that sync, the controller still lists the node's replicas, and a model that
loses its last replica in that window can provision against a dead one and wait on
it indefinitely. So for a planned removal, take the node's replicas off first:

```bash
# which replicas are on the node?
ndif status --verbose | jq '.cluster.nodes[] | select(.name=="<node name>") |
  [.deployments[] | {model_key, replica_id}]'
ndif evict <checkpoint> --replica <replica_id>      # for each; in-flight requests re-queue
# then, on the worker:
ndif stop ray            # or: docker stop ndif-worker
ndif status              # Nodes drops within one sync interval
```

New placements can still land on the node between the evictions and the stop;
check the per-node list once more just before stopping.

After an unplanned loss, if a model's requests sit in `DEPLOYING` (`ndif queue`)
once `ndif status` no longer lists the node, the queue is waiting on a replica
that died with it: restart the API (`just restart api` on the compose stack).
Requests it held in memory are dropped; the next request provisions afresh.

## Gotchas

- **The controller lives on the head.** If you run two heads you have two
  clusters.
- **A node's capacity is read once**, when the controller first sees it: total RAM
  × `NDIF_MODEL_CACHE_PERCENTAGE` (default 0.9) as the WARM budget, and
  `cuda_memory_bytes` divided evenly across its GPUs. Mixed card sizes are
  mis-accounted; to change what a node advertises, take it out and rejoin it.
- **A worker doesn't need `NDIF_REDIS_URL`, `NDIF_OBJECT_STORE_URL` or the
  telemetry variables.** The controller writes its own into every actor's
  `runtime_env` (`src/ndif/services/ray/deployments/controller/cluster/deployment.py`
  `Deployment.create`); setting them on the worker has no effect on the actors.
  The object-store URLs must therefore be reachable from every node.

## Related

- [Ports](../reference/ports.md) — every port, which variable moves it, what
  compose publishes.
- [Ray service](../developing/ray-service.md) — `start.sh` and `resources.py` in
  detail.
- [Deployments and eviction](../concepts/deployments-and-eviction.md) — how the
  controller picks a node.
- [Production](../operating/production.md) — the rest of a multi-node setup.
- [Deploy and pin a model](deploy-and-pin-a-model.md) — put a model on the new
  capacity.
