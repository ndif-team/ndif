"""Coordination for the /status endpoint's coalesced cluster-status cache.

The API serves a TTL'd JSON blob from ``STATUS_KEY``. On a miss, requests
coalesce through ``STATUS_REQUESTED_KEY`` (a SET NX lock) so only one triggers
a refresh: the winner appends to ``STATUS_TRIGGER_STREAM``, which the
dispatcher's status worker consumes. The worker fetches from the controller,
writes ``STATUS_KEY``, and PUBLISHes ``STATUS_READY_CHANNEL`` to wake waiters.
"""

import os

# Redis key holding the latest controller status JSON (TTL'd).
STATUS_KEY = "status"

# Coalescing lock: only the request that SETs this NX triggers a refresh. Its
# TTL doubles as a dead-man's switch so a crashed refresh eventually retries.
STATUS_REQUESTED_KEY = "status:requested"

# Stream the API appends refresh triggers to; the dispatcher's worker reads it.
STATUS_TRIGGER_STREAM = "status:trigger"

# Pub/sub channel the dispatcher publishes to when a refresh finishes ("ok" or
# "error"); waiting /status requests are subscribed to it.
STATUS_READY_CHANNEL = "status:ready"

# How long a cached status is served before a refresh is needed.
STATUS_TTL_S = int(os.environ.get("NDIF_STATUS_TTL_S", "60"))

# How long /status waits for a refresh, and how long the worker bounds the
# controller fetch / holds the coalescing lock.
STATUS_TIMEOUT_S = int(os.environ.get("NDIF_STATUS_TIMEOUT_S", "60"))
