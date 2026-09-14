---
title: Errors
one_liner: Failure-shaped entry points — a symptom or an exception, its real cause, and the page that explains it.
tags: [errors]
related: [docs/errors/client-side-failures.md, docs/errors/server-exceptions.md, docs/operating/troubleshooting.md, docs/gotchas/index.md]
sources: []
---

# Errors

## What this covers

Two pages, split by where you're standing:

- **[Client-side failures](client-side-failures.md)** — a user pasted an error from
  their nnsight session. Organized by what they see: an HTTP status, an `ERROR`
  response, a job stuck in `QUEUED`, a download that fails after `COMPLETED`.
- **[Server exceptions](server-exceptions.md)** — you're reading server logs.
  Organized by exception: what raised it, whether it's user-caused or server-caused,
  and what to do.

For stack-level symptoms that aren't tied to one request — a service that won't
start, a container with no GPU — go to
[Troubleshooting](../operating/troubleshooting.md). For traps that bite before
they produce an error at all, see [Gotchas](../gotchas/index.md).

## First, decide which half you're in

A surprising amount of NDIF debugging is deciding whether a failure is the
**user's code** or the **server**. The distinction is sharp in the code:

- A block that raises is **user-caused** and never fatal to the actor. On the
  untrusted path the runner formats the traceback itself — tracebacks don't
  survive cloudpickle — and it comes back as an `ERROR` response carrying that
  text. The user is reading their own traceback.
- A failure in loading, placement, upload, or transport is **server-caused**, and
  is the kind that can restart an actor or wedge a queue.

If the user's traceback names their own frames, stop looking at the server.

There is a third shape, and it is the one people misread: an `ERROR` whose
`description` is **one prose sentence with no frames**. That is a *rejected*
request — the payload couldn't be read, or its module paths don't resolve against
the server's model — and it is deliberate: every frame at those points is
server-side, so a traceback would leak module layout while telling the caller
nothing (`src/ndif/common/errors.py:19-28`). Traceback ⇒ their code ran and
failed; sentence ⇒ we refused it before their code existed. See
[A sentence instead of a traceback](client-side-failures.md#a-sentence-instead-of-a-traceback).

## Two failures that look like something else

**A job that never gets a status at all** usually isn't a queue problem — it's
that the client never subscribed, or the dispatcher process is dead while the API
still reports healthy. `ray:connected` has no TTL, so `/ping`, `/connected`,
`/status` and `/env` keep answering after the dispatcher dies.

**`COMPLETED` followed by a failed download** is almost always the presigned-URL
host mismatch: the blob is signed with `NDIF_OBJECT_STORE_PUBLIC_URL`, and if that
isn't an address the client can reach, the job succeeded and the download can't.
(A result under `NDIF_MAX_SOCKET_RESULT_BYTES`, 4 MiB, rides back on the response
itself and never touches a URL — so this only bites above that, and always for a
non-blocking job.)

## Before anything else, check the versions

`ndif version` prints the resolved ndif / nnsight / torch (+ CUDA line) /
transformers / ray of a running install, and `ndif doctor` prints the same set
alongside its connectivity probes. A large share of the failures on both pages —
unreadable payloads, unresolvable module paths, a 400 from the version gate — are
a client and a server disagreeing about one of those five. Run it on both sides
(`ndif env` vs `ndif env --local`) before reading a traceback closely. See
[Client/server versions](../gotchas/client-server-versions.md).

## Related

- [Troubleshooting](../operating/troubleshooting.md) — stack-level triage.
- [Debug a Stuck Request](../runbooks/debug-a-stuck-request.md) — the full procedure.
- [Status and Results](../concepts/status-and-results.md) — what each status means.
