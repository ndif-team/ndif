---
title: Runbooks
one_liner: Step-by-step operational procedures — one task per page, with real commands and verification at each step.
tags: [runbook, operating]
related: [docs/operating/index.md, docs/operating/troubleshooting.md, docs/reference/error-messages.md]
sources: []
---

# Runbooks

## What this covers

One task per page, start to finish, with commands and a check at each step. If
you don't yet know which procedure you need, start at
[Troubleshooting](../operating/troubleshooting.md); it routes here.

## The runbooks

| Runbook | Use it when |
|---|---|
| [Add a GPU Node](add-a-gpu-node.md) | Growing the cluster — joining a second GPU machine as a Ray worker, verifying its GPUs are visible, and draining it later. |
| [Deploy and Pin a Model](deploy-and-pin-a-model.md) | Putting a model on the cluster, proving it serves, and exempting it from automatic eviction. |
| [Debug a Stuck Request](debug-a-stuck-request.md) | A user's trace is hung and you need to walk the path from request id to root cause. |
| [Model OOM on Deploy](model-oom-on-deploy.md) | A deploy is refused or the actor dies loading weights. |
| [Enable API-Key Auth](enable-auth.md) | Before anyone else can reach your NDIF. |
| [Trace a User's Failed Job](trace-a-users-failed-job.md) | A user reports an error at a given time and you need to reconstruct what happened. |

## Related

- [Troubleshooting](../operating/troubleshooting.md) — symptom-first triage.
- [Error messages](../reference/error-messages.md) — when a user pastes an
  error at you.
- [The ndif CLI](../operating/cli.md) — the commands these runbooks use.
