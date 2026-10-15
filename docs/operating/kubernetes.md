---
title: Kubernetes (Helm)
one_liner: Install NDIF on Kubernetes with the Helm chart in charts/ndif — the minimum values for one GPU node, cloud S3 versus the in-cluster store and the public-URL trap, adding GPU nodes, and upgrades.
tags: [operating, ray, gotchas]
related: [docs/operating/production.md, docs/operating/configuration.md, docs/runbooks/add-a-gpu-node.md, docs/reference/ports.md, docs/reference/env-vars.md, docs/concepts/services-and-topology.md, docs/runbooks/enable-auth.md]
sources: [charts/ndif/Chart.yaml, charts/ndif/values.yaml, charts/ndif/ci/external-s3-values.yaml, charts/ndif/ci/cpu-smoke-values.yaml, src/ndif/services/ray/start.sh, src/ndif/services/ray/resources.py, src/ndif/services/api/gunicorn_conf.py, src/ndif/services/api/app.py, src/ndif/common/providers/objectstore.py, .github/workflows/helm.yml]
---

# Kubernetes (Helm)

## What this covers

Running NDIF on a Kubernetes cluster with the chart in `charts/ndif`. The chart
runs the published `ndif/ndif` image as plain pods, one service per pod, wired
by the same `NDIF_*` variables as the compose stack. Every value is documented
inline in `charts/ndif/values.yaml`; this page covers the decisions you have to
make. Hardening (auth, the sandbox actor, dashboard credentials) is the same as
anywhere else: read [Going to Production](production.md) first.

## What the chart runs

| Component | Kind | Notes |
|---|---|---|
| api | Deployment (1 replica, `Recreate`) + Service, optional Ingress | Port 8001. Liveness `GET /ping`, readiness `GET /connected`, so the Service routes only to an API that has reached Ray. |
| ray head | Deployment (1 replica, `Recreate`) + ClusterIP Service | GCS, Ray client, the NDIF controller. GPU via `nvidia.com/gpu`. Probes run `ray health-check` against the local GCS. |
| ray workers | Deployment, `ray.workers.replicas` (default 0) | Join the head through its Service name; one per node. |
| redis | Deployment + Service | Queue and caches only, no persistence. `redis.external.url` replaces it. |
| rustfs | StatefulSet with a PVC + Service, optional Ingress | In-cluster S3. `rustfs.enabled=false` for a cloud bucket. |
| dashboard | Deployment + PVC + Service | Off by default (`dashboard.enabled`). |
| HF cache | PersistentVolumeClaim | Mounted at `HF_HOME` on every Ray pod; kept on `helm uninstall`. |

Both Ray pods get a memory-backed `emptyDir` at `/dev/shm` (`ray.shmSize`) for
the object store, and a 60-second termination grace period with a `ray stop`
preStop hook; the API gets the same grace period.

The head is a Deployment, not a StatefulSet: it keeps no state on disk (the GCS
and the controller live in memory, and the HF cache is a shared claim), so it
needs only a stable Service name, not a stable pod identity. `Recreate`
guarantees there are never two heads, which would be two clusters.

Postgres is not in the chart. Point `api.postgres.url` (or `existingSecret`) at
a database you run, created with `docker/postgres/init.sql`; see
[Enable auth](../runbooks/enable-auth.md).

## Install

From a checkout, with Helm 3:

```bash
helm install ndif charts/ndif -n ndif --create-namespace -f my-values.yaml
kubectl -n ndif port-forward svc/ndif-api 18001:8001
curl http://localhost:18001/connected        # {"status":"connected"}
```

The install notes print the same check and warn when auth is off or the object
store has no client-reachable address.

The image tag is `<appVersion>-<image.cuda>`: `image.cuda: cu126` for any CUDA
12.x driver, `cu130` for CUDA 13 drivers (Blackwell). `image.tag` overrides it
outright.

## Minimum values for one GPU node

The defaults already describe one GPU node: a GPU head, no workers, in-cluster
Redis and RustFS. What you add:

```yaml
hfToken:
  value: hf_...                 # or existingSecret, for gated repos
ray:
  deployments: [openai-community/gpt2]   # pinned at controller start
  head:
    gpus: 1
    tolerations: [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
rustfs:
  accessKey: <random>
  secretKey: <random>
  ingress:
    enabled: true
    host: results.example.com   # becomes NDIF_OBJECT_STORE_PUBLIC_URL
api:
  ingress:
    enabled: true
    host: ndif.example.com
    tlsSecretName: ndif-tls
  postgres:
    existingSecret: ndif-postgres   # key NDIF_POSTGRES_URL
```

The cluster needs the NVIDIA device plugin (or GPU operator) so pods can request
`nvidia.com/gpu`; set `ray.runtimeClassName` if your nodes need a runtime class
for GPU containers.

Two sizing rules differ from a bare host:

- **Memory.** The controller sizes its CPU model cache from the *node's* total
  RAM (`src/ndif/services/ray/resources.py` `get_total_cpu_memory_bytes`, times
  `NDIF_MODEL_CACHE_PERCENTAGE`), not from the pod's limit, so the Ray pods carry
  no memory limit by default. If you set one, lower that percentage (via
  `ray.extraEnv`) to fit under it.
- **Shared memory.** `/dev/shm` is a memory `emptyDir`, and what Ray writes there
  counts against the pod's memory.

## Object store: cloud S3 or in-cluster RustFS

Results larger than Redis's inline cap are uploaded to a bucket and handed to
the client as a presigned URL. The client downloads it directly.

**Cloud S3 is the production choice.** Set `rustfs.enabled: false`, leave
`objectStore.url` empty, set `objectStore.region` and `objectStore.bucket`, and
give the pods an identity: annotate `serviceAccount` for IRSA, EKS Pod Identity
or Workload Identity and leave the keys empty, or set `objectStore.accessKey` /
`secretKey` (or `existingSecret`). AWS URLs are public already, so there is
nothing else to expose. `charts/ndif/ci/external-s3-values.yaml` is a complete
example.

**In-cluster RustFS** suits clusters with no cloud bucket. Its data lives on the
StatefulSet's claim.

> **Gotcha: the public URL.** A presigned URL signs the host it names
> (`src/ndif/common/providers/objectstore.py` `ObjectStoreProvider.presigned_get`).
> With RustFS in the cluster, the server uploads through the Service name
> (`http://<release>-rustfs:9000`), which no client can resolve. Expose the store
> through `rustfs.ingress` (its host becomes `NDIF_OBJECT_STORE_PUBLIC_URL`
> automatically) or a LoadBalancer plus `objectStore.publicUrl`. Small results
> still arrive inline, so the mistake shows only on the first large one.

## Add GPU nodes

Raise `ray.workers.replicas` and set `ray.workers.gpus` to the GPUs per node:

```yaml
ray:
  workers:
    replicas: 2
    gpus: 8
    nodeSelector: {nvidia.com/gpu.product: NVIDIA-H100-80GB-HBM3}
hfCache:
  accessModes: [ReadWriteMany]
  storageClass: <an RWX class: EFS, Filestore, NFS, CephFS>
```

Each worker joins `<release>-ray-head:<ray.gcsPort>` as described in
[Add a GPU node](../runbooks/add-a-gpu-node.md); nothing else changes. Pods
share a flat network, so Ray's unpinned ports need no configuration unless a
NetworkPolicy restricts traffic between the Ray pods.

- **One worker per node.** By default a required anti-affinity keeps workers on
  separate nodes, because each one advertises its whole node's RAM; two on one
  node would double-count it. Setting `ray.workers.affinity` replaces that rule.
- **The HF cache must be ReadWriteMany** once Ray pods land on more than one
  node, or the claim cannot attach to them all. With `hfCache.enabled: false`
  each pod downloads weights into its own filesystem on every start instead.
- **Restarting the head drops the cluster.** Workers' raylets exit when the GCS
  goes away; their liveness probe (is a `raylet` process alive) restarts the
  pod, which rejoins the new head. Models redeploy from `ray.deployments` and
  new requests.

Removing a worker: lower `replicas`. As on a bare host there is no drain, so
evict its replicas first (see the runbook's "Take a node out").

## Upgrade

```bash
helm upgrade ndif charts/ndif -n ndif -f my-values.yaml
```

- A new image restarts every NDIF pod. The head restarts with `Recreate`, so
  every loaded model unloads and pinned models reload from the HF cache; plan it
  like a maintenance window. In-flight requests are lost.
- Changing an inline secret restarts only the pods that read it (a checksum
  annotation per component), so a new dashboard password leaves Ray alone.
- Pin an exact `image.tag` for production; `latest` moves.
- The HF cache claim carries `helm.sh/resource-policy: keep`, and the RustFS
  claim belongs to its StatefulSet, so neither is deleted by `helm uninstall`.

## Not in the chart

- **One API pod only.** The queue dispatcher starts once per gunicorn master
  (`src/ndif/services/api/gunicorn_conf.py` `on_starting`), so a second API pod
  on the same Redis would run a second dispatcher. Scale with `api.workers`.
- **KubeRay.** The chart runs the image's own `start.sh` rather than a
  `RayCluster`. KubeRay would add autoscaled workers and GCS fault tolerance, and
  is the natural next step if those matter.
- **Autoscaling** of pods and nodes, **NetworkPolicies**, **TLS** (bring an
  Ingress controller and cert-manager), **Postgres**, and the telemetry stack
  (point `extraEnv` at your own Loki or InfluxDB with `NDIF_LOKI_URL` /
  `NDIF_INFLUX_URL`).
- **Non-root containers.** The image runs as root and caches weights under
  `/root/.cache/huggingface`. For a non-root image, set `podSecurityContext`
  (`runAsUser`, `fsGroup`) and move `hfCache.mountPath`. The dashboard's crons
  need root.

CI lints and renders the chart with its default values and every
`charts/ndif/ci/*-values.yaml` file (`.github/workflows/helm.yml`).
`charts/ndif/ci/cpu-smoke-values.yaml` installs a GPU-less stack on a local
cluster such as kind; the API reports connected, but no model can be placed.

## Related

- [Going to Production](production.md) — auth, the sandbox actor class, and the
  checklist the chart's values map onto.
- [Add a GPU node](../runbooks/add-a-gpu-node.md) — what a worker does when it joins.
- [Ports](../reference/ports.md) — every port the head Service exposes.
- [Environment Variables](../reference/env-vars.md) — everything `extraEnv` can set.
