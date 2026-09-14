"""Coordination for the /env endpoint's coalesced cluster-env cache.

Mirrors :mod:`ndif.common.redis.status`. The API can't reach Ray directly (only the
dispatcher connects), so it serves a TTL'd JSON blob from ``ENV_KEY``. On a
miss, requests coalesce through ``ENV_REQUESTED_KEY`` (a SET NX lock) so only
one triggers a refresh: the winner appends to ``ENV_TRIGGER_STREAM``, which the
dispatcher's env worker consumes. The worker fetches ``env`` from the
controller, writes ``ENV_KEY``, and PUBLISHes ``ENV_READY_CHANNEL`` to wake
waiters.

The cluster env (python version + installed packages) only changes on a
redeploy, so a longer TTL than /status is fine.
"""

import os

# Redis key holding the latest controller env JSON (TTL'd).
ENV_KEY = "env"

# Coalescing lock: only the request that SETs this NX triggers a refresh. Its
# TTL doubles as a dead-man's switch so a crashed refresh eventually retries.
ENV_REQUESTED_KEY = "env:requested"

# Stream the API appends refresh triggers to; the dispatcher's worker reads it.
ENV_TRIGGER_STREAM = "env:trigger"

# Pub/sub channel the dispatcher publishes to when a refresh finishes ("ok" or
# "error"); waiting /env requests are subscribed to it.
ENV_READY_CHANNEL = "env:ready"

# How long a cached env is served before a refresh is needed. Env is ~static
# for a cluster's lifetime, so this is generous.
ENV_TTL_S = int(os.environ.get("NDIF_ENV_TTL_S", "300"))

# How long /env waits for a refresh, and how long the worker bounds the
# controller fetch / holds the coalescing lock.
ENV_TIMEOUT_S = int(os.environ.get("NDIF_ENV_TIMEOUT_S", "60"))
