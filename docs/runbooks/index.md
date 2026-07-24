---
title: Runbooks
one_liner: Step-by-step operational procedures — one task per page, with real commands and verification at each step.
tags: [runbook, operating]
related: [docs/operating/index.md, docs/operating/troubleshooting.md, docs/errors/index.md]
sources: []
---

# Runbooks

## What this covers

One task per page, start to finish, with commands you can actually run and a way
to verify each step. These are the operational analogue of a cookbook: where
[Operating](../operating/index.md) explains how a subsystem works,
a runbook walks you through doing one specific thing with it.

If you don't yet know *which* procedure you need, start at
[Troubleshooting](../operating/troubleshooting.md) — it is triage, and it routes
here.

## The runbooks

| Runbook | Use it when |
|---|---|
| [Add a GPU Node](add-a-gpu-node.md) | Growing the cluster — joining a second GPU machine as a Ray worker, verifying its GPUs are visible, and draining it later. |
| [Deploy and Pin a Model](deploy-and-pin-a-model.md) | Putting a model on the cluster, proving it serves, and exempting it from automatic eviction. |
| [Debug a Stuck Request](debug-a-stuck-request.md) | A user's trace is hung and you need to walk the path from request id to root cause. |
| [Model OOM on Deploy](model-oom-on-deploy.md) | A deploy is refused or the actor dies loading weights. |
| [Enable API-Key Auth](enable-auth.md) | Before anyone else can reach your NDIF. |
| [Trace a User's Failed Job](trace-a-users-failed-job.md) | A user reports an error at a given time and you need to reconstruct what happened. |

## If you only read one

[Enable API-Key Auth](enable-auth.md). Until Postgres-backed verification is on,
NDIF stamps every request `trusted` — user-submitted Python runs in the model
actor process, next to the weights, and models load with `trust_remote_code`. It
is the single highest-consequence default in the system, and the runbook explains
both how to turn it on and what you're exposed to until you do.

## A note on evidence

What you can reconstruct after the fact depends on how the job was submitted, and
this shapes several of these runbooks:

- **Blocking jobs** (a `session_id`, the normal case) stream status over Redis
  pub/sub to a websocket and store **nothing**.
- **Non-blocking jobs** keep only the **latest** response, in the object store.
- **Result blobs** persist indefinitely — nothing in NDIF deletes them — but
  presigned URLs expire after an hour.
- **In-flight requests live only in the dispatcher's memory.** Restarting the API
  drops them silently, including for clients waiting on a websocket.

## Related

- [Troubleshooting](../operating/troubleshooting.md) — symptom-first triage.
- [Client-side failures](../errors/client-side-failures.md) — when a user pastes an
  error at you.
- [The ndif CLI](../operating/cli.md) — the commands these runbooks use.
