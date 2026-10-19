---
title: Deploy and Pin a Model
one_liner: Put a model on the cluster under the model key clients will ask for, prove it serves, pin it without downtime, keep it coming back, and remove it.
tags: [runbook, operating, controller, cli, dashboard]
related: [docs/operating/models-and-deployment.md, docs/operating/cli.md, docs/operating/dashboard.md, docs/concepts/deployments-and-eviction.md, docs/concepts/auth-and-limits.md, docs/developing/controller-internals.md, docs/runbooks/model-oom-on-deploy.md, docs/runbooks/debug-a-stuck-request.md, docs/reference/env-vars.md]
sources: [src/ndif/cli/commands/deploy.py, src/ndif/cli/lib/deploy.py, src/ndif/cli/lib/models.py, src/ndif/cli/lib/model_config.py, src/ndif/cli/lib/evict.py, src/ndif/cli/commands/export.py, src/ndif/common/schema/controller.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/api/queue/replica.py, src/ndif/services/dashboard/backend/routers/deployments.py, src/ndif/services/dashboard/backend/routers/schedule.py, src/ndif/services/dashboard/jobs/reconcile.py]
---

# Deploy and Pin a Model

## What this covers

Getting one model from nothing to "serving and staying put", with a check after
each step. Three facts shape the procedure:

1. **You usually don't have to deploy anything.** The first request for a model
   with no HOT replica makes the queue ask the controller for one
   (`src/ndif/services/api/queue/replica.py` `Replica.provision`). Deploy
   explicitly so the model is already loaded when the first user arrives, and so
   it can be pinned.
2. **Deploy is additive.** `replicas` means "place this many *new* replicas"
   (`src/ndif/services/ray/deployments/controller/cluster/cluster.py`
   `Cluster.deploy`). `ndif deploy gpt2` three times gives three replicas. Only
   `ndif deploy -f models.yaml --sync` reconciles to a count.
3. **The model key includes the pipeline task.** A client's key is
   `TransformersModel:{"repo_id": …, "revision": …, "task": …}`, with the task
   always resolved. A deployment serves only requests whose key matches exactly, so
   a model deployed under another task — or under a key with no task — is a
   separate deployment that no default client request reaches.

`ndif` commands run wherever the CLI reaches `NDIF_RAY_ADDRESS`. On the compose
stack, run them in the `ray` container:

```bash
# a function, not an alias: works in scripts and from any directory (project `dev`)
ndif() { docker exec -i dev-ray-1 ndif "$@"; }
```

## 1. Deploy

```bash
ndif deploy openai-community/gpt2
```

```
Generating model key for openai-community/gpt2...
  Model key: nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", "revision": null, "task": "text-generation"}
  Task not given; inferred 'text-generation' from the checkpoint
Connecting to Ray at ray://localhost:10001...

Deploying 1 model(s)...
  ⋯ nnsight.modeling.transformers.TransformersModel:{...}: provisioned 1 replica(s), initializing...
      - [a1b2c] waiting for ready
  ✓ nnsight.modeling.transformers.TransformersModel:{...} [a1b2c]: ready
```

What happened (`src/ndif/cli/lib/deploy.py` `deploy`):

1. The checkpoint became a **model key** by constructing the nnsight wrapper on
   the meta device (`src/ndif/cli/lib/models.py` `get_model_key`): a Hub lookup,
   no weights. The task is inferred exactly as a client with no `task=` infers it,
   so the two keys match. A typo fails here.
2. The controller sized the model, picked a node, evicted what it had to, and
   recorded a HOT deployment — [Model OOM on deploy](model-oom-on-deploy.md) has
   the arithmetic.
3. It created a detached Ray actor named `{replica_id}:ModelActor:{model_key}` in
   the `NDIF` namespace, which loads the weights.
4. The CLI waits for each replica to be ready, with no deadline. A large model
   sits here while it loads; a model that can't load prints the actor's error. A
   failed deploy exits non-zero.

Useful flags (full list in [The ndif CLI](../operating/cli.md)): `--task`,
`--replicas N`, `--revision`, `--pinned`, `--trusted`, `--dtype`,
`--actor-class`, `-f models.yaml`.

**`--task`** deploys a non-default task. Clients must then load with the same
task (`TransformersModel(repo, task="…")`), or their requests provision a second,
default-task deployment.

**`--trust-remote-code`** lets the model run its own modelling code
(`trust_remote_code=True`, needed for repos that ship it). It is part of the model
key, so clients must load with `trust_remote_code=True` too, and it needs
**`--trusted`**, which marks the deployment as made by a trusted caller; a plain
`ndif deploy` is untrusted and such a key is refused. Dashboard deploys default to trusted and take
`"trusted": false` per request
(`src/ndif/services/dashboard/backend/routers/deployments.py` `DeployRequest`);
scheduled ones are always trusted. What "trusted" means beyond loading is in
[Auth and limits](../concepts/auth-and-limits.md).

## 2. Confirm it is serving

```bash
ndif status
```

```
Active Deployments:

  🔥 HOT (1)
    • openai-community/gpt2
      RUNNING | text-generation | 137M params
```

`HOT` is the controller's bookkeeping (it believes the replica holds GPU memory);
`RUNNING` is the Ray actor's state. `DEPLOYING` means still loading. `UNHEALTHY`
means the actor is dead while the controller still holds the deployment — see
[Debug a stuck request](debug-a-stuck-request.md), step 4.

Per replica:

```bash
ndif status --json-output | jq '.deployments[] | select(.deployment_level=="HOT") |
  {repo_id, task, replica_id, state: .application_state, pinned, trusted, dtype}'
```

**The real proof is a request.** Nothing above runs a forward pass:

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:8001"

from nnsight.modeling.transformers import TransformersModel
model = TransformersModel("openai-community/gpt2")

with model.trace("The Eiffel Tower is in the city of", remote=True):
    hidden = model.transformer.h[-1].output.save()
print(hidden.shape)
```

Watch it land with `ndif queue --watch`: the processor line reads
`openai-community/gpt2 (text-generation)`, `READY`, one replica.

## 3. Pin it without downtime

Pinning is fixed when a replica is created, so pin by adding a pinned replica and
then removing the unpinned one:

```bash
ndif deploy openai-community/gpt2 --pinned          # adds a pinned replica, e.g. [d4e5f]
ndif status --json-output | jq '.deployments[] | select(.repo_id=="openai-community/gpt2") |
  {replica_id, pinned, level: .deployment_level}'
ndif evict openai-community/gpt2 --replica a1b2c    # the old, unpinned one
```

The queue picks up the new replica as soon as the deploy returns, so requests are
served throughout; a request running on `a1b2c` when it is evicted is re-queued.
An evicted HOT replica is demoted to WARM if the node has CPU room; run the same
`evict` again to drop the WARM copy too.

The pinned deploy needs room for a second copy next to the first — another
replica of the same model is never evicted to make it. If it comes back
`CANT_ACCOMMODATE`, accept a gap: evict first, then deploy `--pinned`.

**What pinned does:** the controller never picks a pinned replica as an eviction
victim when making room for another model (`src/ndif/services/ray/deployments/controller/cluster/node.py`
`Node.evictable`), and a pinned deploy may evict unpinned replicas younger than
`NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS`, which an unpinned deploy may not.
If nothing else can be freed, the *incoming* model is refused instead.

**What it doesn't:** it doesn't stop `ndif evict` (including `--all`); it doesn't
bring a dead actor back; it doesn't survive a controller restart, which rebuilds
deployments only from `NDIF_DEPLOYMENTS` and the dashboard's schedule; and it
doesn't reserve room for more replicas. The eviction model is in
[Deployments and eviction](../concepts/deployments-and-eviction.md).

## 4. Make it come back on its own

Three mechanisms, in increasing order of durability.

**`NDIF_DEPLOYMENTS`** — a `|`-separated list of **full model keys** deployed
pinned when the controller starts
(`src/ndif/services/ray/deployments/controller/controller.py`
`ControllerDeploymentArgs`). Copy the keys from a running deployment so they carry
the task:

```bash
ndif status --json-output | jq -r '.deployments[] | select(.deployment_level=="HOT") | .model_key'
```

> **Warning:** a key without `"task"` still deploys, but under a key no client request matches: the
> model shows HOT and the first request provisions a second deployment beside it.
> `ndif evict <checkpoint>` computes the key with the inferred task, so it
> reports `nothing to evict` for such a deployment; evict it by key (below).

It is read by the `ray` service at controller start; on the compose stack, set it
in the `ray` service's `environment:` and recreate with `just up ray`, which takes
every model actor down.

**`ndif deploy -f models.yaml --sync`** — evicts every HOT model key not in the
file, trims replicas above each requested count, and deploys only the shortfall
(`src/ndif/cli/lib/deploy.py` `_sync_reconcile`). Format
(`src/ndif/cli/lib/model_config.py`):

```yaml
models:
  - openai-community/gpt2                 # task inferred, as a client would
  - checkpoint: meta-llama/Llama-3.1-8B
    task: text-generation                 # part of the model key
    pinned: true
    replicas: 2
```

An entry without `task:` takes `--task` when given, else the inferred task, which
matches clients that don't pass one. A `model_key:` entry is used verbatim — it
needs the task in it.
`ndif export --stdout > models.yaml` writes the current HOT set, task and key
included, so "snapshot now, restore later" is two commands. (`-f` writes inside
the `ray` container when the CLI runs there.) Only non-default fields are
written, and replicas with different settings under one key are merged into one
entry, so dump `ndif status --json-output` first if per-replica state matters.

**The dashboard schedule** — the only mechanism that keeps re-asserting itself.

## 5. Schedule it from the dashboard

The dashboard's Schedule tab stores entries in `schedule.json` under
`NDIF_DASHBOARD_DATA_DIR`: a checkpoint (and optional task) plus a `[start, end)`
window; no `end` means indefinitely. Each write resolves the checkpoint through
the Hub and stores the resulting model key, returning **400** if it can't
(`src/ndif/services/dashboard/backend/routers/schedule.py` `_canonicalize`).

The reconcile job (`src/ndif/services/dashboard/jobs/reconcile.py`) runs from cron
every `NDIF_DASHBOARD_RECONCILE_CRON` (default `*/2 * * * *`) and right after any
schedule write. It deploys every active entry that is new or no longer HOT,
pinned and trusted, and evicts model keys that were active on the previous pass
and no longer are.

Run a pass by hand:

```bash
docker compose -f docker/docker-compose.yml exec dashboard \
  python -m ndif.services.dashboard.jobs.reconcile
```

```json
{"changed": true, "active": ["openai-community/gpt2"], "evicted": [], "deployed": ["nnsight.modeling.transformers.TransformersModel:{...}"]}
```

`--force` re-pushes every active entry. Cron output goes to
`$NDIF_DASHBOARD_DATA_DIR/logs/reconcile.cron.log`. `{"skipped":
"controller_status_unavailable"}` means the controller was unreachable and nothing
was done; the next tick retries. It skips rather than acts because every
scheduled model would otherwise look missing and get a second pinned replica.

## Removing it

```bash
ndif evict openai-community/gpt2                   # every HOT and WARM replica
ndif evict openai-community/gpt2 --replica a1b2c   # one
ndif evict openai-community/gpt2 --task fill-mask  # a non-default task's deployment
ndif evict --all                                   # every HOT model, pinned included
```

```
Connecting to Ray at ray://localhost:10001...
  Model key for openai-community/gpt2: nnsight.modeling.transformers.TransformersModel:{...}
  Task not given; inferred 'text-generation' from the checkpoint
Evicting from 1 model(s)...
  ✓ nnsight.modeling.transformers.TransformersModel:{...}: evicted 1 replica(s)
      - [a1b2c] 1 GPU(s), 0.6234 GB
```

`ndif evict <checkpoint>` acts on the inferred task's key only. For a deployment
under any other key — another task you don't know, or a key with no task — evict
by the exact key from `ndif status --json-output`:

```bash
docker compose -f docker/docker-compose.yml exec ray python -c "
from ndif.cli.lib.evict import evict
evict(model_keys=['<model_key>'], on_message=print)"
```

If the model is on a dashboard schedule, remove the entry too, or the next
reconcile puts it back.

## Related

- [Deployments and eviction](../concepts/deployments-and-eviction.md) — placement,
  HOT/WARM/COLD, and the memory ledger.
- [Models and deployment](../operating/models-and-deployment.md) — `models.yaml`,
  revisions, dtype, PEFT, actor classes.
- [The ndif CLI](../operating/cli.md) — every flag of `deploy`, `evict`,
  `restart`, `export`.
- [Dashboard](../operating/dashboard.md) — the UI, its login, and the crons.
- [Model OOM on deploy](model-oom-on-deploy.md) — when the deploy fails or the
  actor dies.
