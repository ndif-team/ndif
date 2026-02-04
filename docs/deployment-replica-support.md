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
The system-wide replica count is set in `src/services/api/src/queue/config.py` with the variable `NDIF_MODEL_REPLICAS_DEFAULT`. This value is used as the default number of replicas for a model when a user issuing a model tracer without existing deployments (defaults to 1). 
The replica deployments may fail if there are not enough resources to deploy the replicas. A request which deploys a model will be served as long as `NDIF_PROCESSOR_MIN_READY_REPLICAS` (in `src/services/api/src/queue/config.py`, defaults to 1) replicas are successfully deployed. 
In this version, no auto-scaling is supported, so the replica can only be controlled by admin using cli commands.

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

