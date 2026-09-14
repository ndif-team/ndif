---
title: Deploy and Pin a Model
one_liner: Put a model on the cluster, prove it is actually serving, mark it exempt from automatic eviction, and give it a schedule in the dashboard.
tags: [runbook, operating, controller, cli, dashboard]
related: [docs/operating/models-and-deployment.md, docs/operating/cli.md, docs/operating/dashboard.md, docs/concepts/deployments-and-eviction.md, docs/developing/controller-internals.md, docs/runbooks/model-oom-on-deploy.md, docs/runbooks/debug-a-stuck-request.md, docs/reference/env-vars.md]
sources: [src/ndif/cli/commands/deploy.py, src/ndif/cli/lib/deploy.py, src/ndif/cli/lib/models.py, src/ndif/cli/lib/model_config.py, src/ndif/cli/lib/evict.py, src/ndif/common/schema/controller.py, src/ndif/services/ray/deployments/controller/cluster/cluster.py, src/ndif/services/ray/deployments/controller/cluster/node.py, src/ndif/services/ray/deployments/controller/controller.py, src/ndif/services/api/queue/processor.py, src/ndif/services/dashboard/backend/routers/schedule.py, src/ndif/services/dashboard/jobs/reconcile.py]
---

# Deploy and Pin a Model

## What this covers

Getting one model from "nothing" to "serving and staying put", with a check after
each step, plus what pinning actually buys you. Two facts shape the whole
procedure.

1. **You usually don't have to deploy anything.** The queue provisions on demand:
   the first request for a model that has no HOT replica makes the Processor ask
   the controller to deploy one (`queue/processor.py:198`,
   `queue/replica.py:102-110`). Explicit deploy exists so a model is *already
   warm* when the first user arrives, and so it can be pinned.
2. **Deploy is additive, always.** `DeploymentConfig.replicas` means "place this
   many *new* replicas", regardless of what is already running
   (`common/schema/controller.py:20-26`, `cluster/cluster.py:194`). Running
   `ndif deploy gpt2` three times gives you three replicas. The only reconciling
   form is `ndif deploy -f models.yaml --sync`.

## 1. Deploy

```bash
ndif deploy openai-community/gpt2
```

`deploy` talks to the Ray controller, so it runs from anywhere the CLI is installed
and `NDIF_RAY_ADDRESS` is reachable — the host if you started NDIF with `ndif start`,
or inside the container for the compose stack
(`docker compose -f docker/docker-compose.yml exec ray ndif deploy ...`).

Real output:

```
Generating model key for openai-community/gpt2...
  Model key: nnsight.modeling.transformers.TransformersModel:{"repo_id": "openai-community/gpt2", ...}
Connecting to Ray at ray://localhost:10001...

Deploying 1 model(s)...
  ⋯ nnsight.modeling.transformers.TransformersModel:{...}: provisioned 1 replica(s), initializing...
      - [a1b2c] waiting for ready
  ✓ nnsight.modeling.transformers.TransformersModel:{...} [a1b2c]: ready
```

What happened, in order (`src/ndif/cli/lib/deploy.py:64-193`):

1. The checkpoint is canonicalized to a **model_key** by constructing the nnsight
   wrapper class on the meta device — no weights, but a HuggingFace Hub lookup
   (`cli/lib/models.py:16-27`). Default wrapper:
   `nnsight.modeling.transformers.TransformersModel`. This is the identity
   everything downstream compares on, and it is why a typo fails here rather than
   silently later.
2. The controller sizes the model on the meta device, picks a node, evicts
   whatever it must to make room, and records a HOT `Deployment`
   (`cluster/cluster.py:143`).
3. The controller creates a **detached Ray actor** named
   `{replica_id}:ModelActor:{model_key}` in the `NDIF` namespace
   (`cluster/deployment.py:105`, `:192-198`). Weights load inside that actor.
4. The CLI blocks per replica on `__ray_ready__` with no deadline
   (`cli/lib/models.py`). A large model simply sits here while it loads; if it
   cannot come up, the actor's own error is what you get, not a timeout.

Useful flags (full list in [docs/operating/cli.md](../operating/cli.md)):
`--replicas N`, `--revision`, `--pinned`, `--actor-class`, `--trusted`, `--dtype`,
and `-f models.yaml`.

> **Note:** a CLI deploy can be *trusted*. `ndif deploy --trusted` sets it and
> `load_model_config` reads a `trusted:` key from `models.yaml`
> (`cli/lib/model_config.py`), so a CLI or YAML deploy can load with
> `trust_remote_code=True` and serve architectures that need repo code. It
> defaults off — a plain `ndif deploy` loads with `trust_remote_code=False`. The
> dashboard's deploy endpoints hard-code `trusted: True`
> (`dashboard/backend/routers/deploy.py:34`). See
> [docs/concepts/auth-and-limits.md](../concepts/auth-and-limits.md) for what
> "trusted" means beyond model loading.

## 2. Confirm it is serving

`ndif status` is the first check:

```bash
ndif status
```

```
Active Deployments:

  🔥 HOT (1)
    • openai-community/gpt2
      RUNNING | 124M params
```

Read that line carefully. `HOT` is the controller's *bookkeeping* — it means the
controller believes the replica holds GPU memory. `RUNNING` is the **Ray actor
state**, derived from `list_actors()`: `ALIVE` → `RUNNING`, `PENDING_CREATION` /
`RESTARTING` / `DEPENDENCIES_UNREADY` → `DEPLOYING`, `DEAD` → `UNHEALTHY`
(`controller.py:395-419`). `HOT` with `DEPLOYING` means the weights are still
loading; `HOT` with `UNHEALTHY` means the actor died and the controller hasn't
reconciled yet — go to
[docs/runbooks/model-oom-on-deploy.md](model-oom-on-deploy.md).

Per-replica and per-node detail:

```bash
ndif status --json-output | jq '.deployments | to_entries[] |
  {actor: .key, level: .value.deployment_level, state: .value.application_state,
   pinned: .value.pinned, trusted: .value.trusted, replica: .value.replica_id}'
```

**The real proof is a request.** Nothing above executes a forward pass:

```python
import nnsight
nnsight.CONFIG.API.HOST = "http://localhost:8001"

from nnsight.modeling.transformers import TransformersModel
model = TransformersModel("openai-community/gpt2")

with model.trace("The Eiffel Tower is in the city of", remote=True):
    hidden = model.transformer.h[-1].output.save()
print(hidden.shape)
```

Watch it land with `ndif queue --watch` in another terminal — you should see the
Processor for that model go `READY` with one replica and a queue depth of 0
between requests.

## 3. Pin it

Pinning is a property of the deployment, set at deploy time:

```bash
ndif evict openai-community/gpt2      # additive deploy means: remove first
ndif deploy openai-community/gpt2 --pinned
```

Confirm:

```bash
ndif status --json-output | jq '.deployments[] | select(.repo_id=="openai-community/gpt2") | .pinned'
# true
```

### What pinned does

`pinned` is a single boolean on the `Deployment` (`cluster/deployment.py:65`,
`:78`) and it is consulted in exactly one place:
`Node.evictable` returns `False` for any pinned deployment
(`cluster/node.py:314-317`). `find_evictions` builds its candidate set only from
evictable deployments (`node.py:350`), so a pinned replica is never chosen as the
victim when the controller is making room for another model. If nothing else can
be freed, the incoming model gets `CANT_ACCOMMODATE` and *its* deploy fails
instead.

Pinned also exempts the deployment from the `minimum_deployment_time_seconds`
display in `status()` (`controller.py:458-466`) — that field is a countdown for
unpinned deployments only.

### What pinned does not do

- **It does not stop `ndif evict`.** `Cluster.evict` never looks at `pinned`
  (`cluster/cluster.py:268`); an explicit evict — including `ndif evict --all` —
  removes pinned replicas like any other.
- **It does not survive a restart of the head.** The controller's state is
  in-memory. If the controller actor is replaced, the deployment set is rebuilt
  from `NDIF_DEPLOYMENTS` (pinned, `controller.py:90-91`) and from whatever the
  dashboard's reconcile cron re-pushes — not from what was pinned before.
- **It does not keep the actor alive.** If the model actor dies (OOM, a fatal
  CUDA error, the node going away), pinning doesn't bring it back. The actor
  itself is `max_restarts=-1`, so Ray restarts the process, and it reloads the
  weights.
- **It does not reserve capacity for more replicas.** It protects the replicas
  that exist.

The complementary lever is `NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS` (default 3600):
an *unpinned* deployment younger than that is also not evictable — but only by
another *unpinned* deploy. A pinned deploy ignores the age guard
(`node.py:318-324`). That asymmetry is what lets an operator push a pinned model
in immediately while ordinary demand-driven deploys wait their turn.

## 4. Make it come back on its own

Three mechanisms, in increasing order of durability.

**`NDIF_DEPLOYMENTS`** — a `|`-separated list of **model_keys** (not checkpoints)
deployed pinned when the controller actor starts
(`controller.py:90-91`, `:533`). Get the exact key from
`ndif status --json-output | jq -r '.deployments[].model_key'`. This is the
zero-dependency option; it only fires on controller start.

**`ndif deploy -f models.yaml --sync`** — the only command that reconciles rather
than adds. It evicts every HOT model_key not in the file, trims replicas above
the requested count, and deploys only the shortfall
(`cli/lib/deploy.py:196-250`). The file format
(`cli/lib/model_config.py:1-12`):

```yaml
models:
  - openai-community/gpt2
  - checkpoint: meta-llama/Llama-3.1-8B
    revision: null
    pinned: true
    replicas: 2
```

`ndif export -f models.yaml` writes this file from the current HOT set, so
"snapshot now, restore later" is a two-command loop.

**The dashboard schedule** — the only mechanism that keeps re-asserting itself.

## 5. Schedule it from the dashboard

The dashboard (`http://localhost:8081`) has a Schedule tab backed by
`schedule.json` under `NDIF_DASHBOARD_DATA_DIR`. Each entry is a checkpoint plus
a `[start, end)` window; an entry with no `end` is open-ended — that is how the
UI expresses "keep this up indefinitely"
(`dashboard/backend/schedule_store.py:53-69`).

On every write the router canonicalizes the checkpoint through HuggingFace and
stamps the resulting `model_key` onto the entry, returning **400** if the repo
can't be resolved (`routers/schedule.py:18-43`) — so a typo is caught at write
time, not at the next tick.

The reconcile job then does the work (`dashboard/jobs/reconcile.py`):

- runs from cron every `NDIF_DASHBOARD_RECONCILE_CRON` (default `*/2 * * * *`),
  and immediately as a background task after any schedule write
  (`routers/schedule.py:55-65`);
- reads the currently-active entries, reads the controller's live HOT set, and
  deploys anything active that is missing — covering both "newly scheduled" and
  "was deployed, has since drifted out of HOT" (`reconcile.py:224-230`);
- evicts model_keys that were active on the previous pass and no longer are
  (`reconcile.py:201`);
- deploys every entry with `pinned: True` **and** `trusted: True`
  (`reconcile.py:57-67`).

Verify a schedule took effect:

```bash
# inside the dashboard container
python -m ndif.services.dashboard.jobs.reconcile
```

```json
{"changed": true, "active": ["openai-community/gpt2"], "evicted": [], "deployed": ["nnsight.modeling.transformers.TransformersModel:{...}"]}
```

Add `--force` to re-push every active entry even when the persisted state and the
controller agree. Logs land in `$NDIF_DASHBOARD_DATA_DIR/logs/reconcile.cron.log`.

> **Gotcha:** if the controller is unreachable, a normal reconcile pass **skips**
> rather than acting (`reconcile.py:214-222`). Acting blind would make every
> scheduled model look like it had drifted out of HOT and stack a second pinned
> replica on top of the one already serving. `{"skipped": "controller_status_unavailable"}`
> in the output means "Ray was down, nothing was done, it'll retry".

## Removing it

```bash
ndif evict openai-community/gpt2              # every HOT and WARM replica
ndif evict openai-community/gpt2 --replica a1b2c   # just one
ndif evict --all                              # every HOT deployment, pinned included
```

Eviction reports what it freed:

```
  ✓ nnsight.modeling.transformers.TransformersModel:{...}: evicted 1 replica(s)
      - [a1b2c] 1 GPU(s), 0.6234 GB
```

Evicting a HOT replica releases its GPU memory and **demotes it to WARM on the
same node** if there is CPU headroom, keeping the weights in host RAM for a fast
re-promotion; otherwise the replica is dropped outright
(`cluster/node.py:250-312`). `ndif evict` without `--replica` drains both the HOT
and WARM copies (`cluster.py:296-315`). If the model is in a dashboard schedule,
evict it there too — otherwise the next reconcile tick puts it straight back.

## Related

- [docs/concepts/deployments-and-eviction.md](../concepts/deployments-and-eviction.md)
  — the placement scoring, the HOT/WARM/COLD tiers, and the memory ledger.
- [docs/operating/models-and-deployment.md](../operating/models-and-deployment.md)
  — `models.yaml`, revisions, dtype, PEFT, actor classes.
- [docs/operating/cli.md](../operating/cli.md) — every flag of `deploy`,
  `evict`, `restart`, `export`.
- [docs/operating/dashboard.md](../operating/dashboard.md) — the UI, its own
  auth, and the crons.
- [docs/runbooks/model-oom-on-deploy.md](model-oom-on-deploy.md) — when the
  deploy above fails or the actor dies.
