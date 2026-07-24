"""The dispatcher's operational event stream (CLI-driven).

Unlike the coalesced ``/status`` and ``/env`` caches, these events act on the
dispatcher's *own* in-memory state (its Processors), not the controller:

  - ``queue_state_request`` / ``kill_request`` are request/response — the caller
    supplies a unique ``response_key`` and blocks (``brpop``) for a JSON reply
    the dispatcher ``lpush``es back.
  - ``reconcile_model`` is fire-and-forget — it nudges the matching Processor to
    re-sync its replica pool against the controller after an out-of-band
    deploy/evict.

The CLI (``ndif queue`` / ``ndif kill`` / ``ndif deploy`` / ``ndif evict``)
produces these; the dispatcher's ``events_worker`` consumes them.
"""

# Stream the CLI appends operational events to; the dispatcher's worker reads it.
EVENTS_STREAM = "dispatcher:events"

# Event types carried on the stream (the ``event_type`` field of each entry).
EVENT_QUEUE_STATE = "queue_state_request"
EVENT_KILL = "kill_request"
EVENT_RECONCILE = "reconcile_model"

# TTL applied to a response_key after the reply is pushed, so an orphaned reply
# (caller already timed out) is reaped instead of leaking.
EVENT_RESPONSE_TTL_S = 30
