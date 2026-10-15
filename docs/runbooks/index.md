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
| [Add a GPU Node](add-a-gpu-node.md) | Growing the cluster — joining another GPU machine as a Ray worker, verifying the controller sees its GPUs, and taking it out again. |
| [Deploy and Pin a Model](deploy-and-pin-a-model.md) | Putting a model on the cluster under the key clients ask for (task included), proving it serves, pinning it without downtime, and removing it. |
| [Debug a Stuck Request](debug-a-stuck-request.md) | A user's trace is hung — walk from request id to root cause, then kill it or restart the replica or API. |
| [Model OOM on Deploy](model-oom-on-deploy.md) | A deploy is refused (`CANT_ACCOMMODATE`), the actor dies loading weights, or requests OOM — and how to size padding. |
| [Enable API-Key Auth](enable-auth.md) | Before anyone else can reach your NDIF — keys in Postgres, keeping them across `just down`, and the dashboard login. |
| [Trace a User's Failed Job](trace-a-users-failed-job.md) | A user reports an error at a given time — find the request, tell their bug from yours, and what to tell them. |

## Related

- [Troubleshooting](../operating/troubleshooting.md) — symptom-first triage.
- [Error messages](../reference/error-messages.md) — when a user pastes an
  error at you.
- [The ndif CLI](../operating/cli.md) — the commands these runbooks use.
