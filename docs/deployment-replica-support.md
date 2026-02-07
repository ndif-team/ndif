# Deployment Replica Support

Introduces how replicas of model deployments are managed in this version.

## What a Replica Is

A replica is a distinct model actor instance running under a shared model key. Each replica has its own `replica_id` and Ray actor name:

```
ModelActor:<model_key>:<replica_id>
```

Replica IDs are integers assigned per model key. When adding replicas, new IDs are assigned after the highest existing ID. 

## Replica Lifecycle

Replicas move between three deployment levels:
- **HOT**: actively running on GPUs and serving requests.
- **WARM**: cached on CPU, ready to reload to GPUs.
- **COLD**: downloaded but not deployed.

## Replica Count and Scaling
Request-driven processors default to a single replica unless an explicit replica
count is provided (for example via `ndif deploy` or `ndif scale`). The replica
deployments may fail if there are not enough resources to deploy all replicas.
A request which deploys a model will be served as long as at least one replica
is successfully deployed. In this version, no auto-scaling is supported, so the
replica count can only be controlled by admin using CLI commands.

## Replica Operations

### Deploy (create initial replicas)

```
ndif deploy <model_key> --replicas <N> --ray-address <ray_address>
```

Behavior:
- Creates `N` replicas (default `1`) for a model that has no active replicas.
- If replicas already exist, deploy is rejected; use `ndif scale` instead.

### Scale to Target

```
ndif scale <model_key> --replicas <N> --ray-address <ray_address>
```

Behavior:
- Ensures the model has **at least** `N` replicas.
- If current replicas already >= `N`, no changes are made.
- Scaling down is not performed.

### Scale Up by Delta

```
ndif scale <model_key> --replicas <N> --ray-address <ray_address> [--scale-up] [--dedicated]
```

Behavior:
- Adds replicas to reach the target count `N`, if current replicas are less than `N`.
- If current replicas are greater than or equal to `N`, no changes are made.
- Scaling down is not performed.
- When `--scale-up` is specified, add `N` more replicas on top of current replicas.
- Scale can partially succeed if there are not enough resources to deploy the replicas.

### Evict Replicas

```
ndif evict <checkpoint> [--replica-id N]
ndif evict --all
```

Behavior:
- Without `--replica-id`, evicts all replicas for the model.
- With `--replica-id`, evicts only that replica.
- `--all` evicts all hot deployments across all models.

### Restart a Replica

```
ndif restart <checkpoint> [--replica-id N]
```

Behavior:
- Restarts the specific replica actor.

## TODOs (tentative, by priority)
- Auto-scaling support.
- Load balancing and placement strategy for replicas.
- Fast replica deployment and restoration, we can probably use existing replica to speed up the deployment and restoration for new replicas.
- Replica with diverse inference backend.
- Smart routing.
