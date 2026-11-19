"""The per-model request queue: FIFO within two priority groups.

Priority used to be implemented by pushing the request onto the *front* of a
plain FIFO. That made the priority group LIFO, which starves under load: a
closed-loop client that wins the head keeps winning it. Measured on a 16-client
run, two priority clients completed 73 requests each while the other fourteen
completed one apiece. It also blinded the autoscaler, which read the head's
wait to decide whether to scale — and the head was always the *newest* prepended
request, with a wait of roughly zero.

So ordering is a key rather than an insertion side::

    (rank, enqueued_at, seq)

    rank 0  priority, re-queued      rank 2  normal, re-queued
    rank 1  priority                 rank 3  normal

The group is doubled so ``prepend`` can be a sub-rank inside it; a plain "+1"
would collide a re-queued normal request with a fresh priority one. Within every
tier the tiebreak is ``enqueued_at``, so each tier is FIFO and nothing starves —
including the re-queue tiers, whose occupants are bounded by the number of
in-flight requests.

``seq`` is a monotonic counter. It never decides real ordering (``enqueued_at``
is effectively unique) but guarantees the tuple comparison stops before it
reaches the ``BackendRequestModel``, which is not orderable.

Re-queues would in fact sort to the front of their group anyway, since a
re-queued request keeps its original ``enqueued_at``. The explicit rank is kept
so that behaviour is stated rather than emergent from timestamp preservation.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from typing import List, Optional

from ....common.schema.request import BackendRequestModel


class RequestQueue:
    """An ``asyncio.PriorityQueue`` of requests, keyed as described above.

    Wraps the queue rather than exposing it: every caller that used to reach
    into ``_queue`` needs the *service* order, and a heap's list is only
    partially sorted, so reading it directly gives a plausible-looking wrong
    answer. The heap lives behind ``snapshot`` / ``oldest`` / ``remove``.
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()

    @staticmethod
    def rank(request: BackendRequestModel, prepend: bool) -> int:
        """Tier for a request: group doubled, minus one if it goes in front."""
        group = 0 if request.priority else 1
        return group * 2 + (0 if prepend else 1)

    def put(self, request: BackendRequestModel, prepend: bool = False) -> None:
        """Enqueue a request. ``prepend`` puts it ahead of its own group only."""
        enqueued_at = request.enqueued_at
        if enqueued_at is None:
            enqueued_at = request.enqueued_at = time.time()
        key = (self.rank(request, prepend), enqueued_at, next(self._seq))
        # put_nowait wakes a waiting getter itself, which the old prepend path
        # had to do by hand.
        self._queue.put_nowait((key, request))

    async def get(self) -> BackendRequestModel:
        """Block until a request is available and return the next to serve."""
        _, request = await self._queue.get()
        return request

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def snapshot(self) -> List[BackendRequestModel]:
        """Queued requests in the order they will actually be served."""
        return [request for _, request in sorted(self._queue._queue, key=lambda i: i[0])]

    def oldest(self) -> Optional[BackendRequestModel]:
        """The request that has waited longest, across *both* groups.

        Not the head: the head is the oldest of the *priority* group, and a
        starved normal request can be far older. This is what the autoscaler
        needs, and it is a scan rather than a maintained watermark — O(n) on a
        queue of tens, once per autoscaling tick, against a bookkeeping
        invariant that every enqueue, dequeue and re-queue would have to honour.
        """
        items = self._queue._queue
        if not items:
            return None
        return min((request for _, request in items),
                   key=lambda r: r.enqueued_at if r.enqueued_at is not None else 0.0)

    def position(self, request_id: str) -> Optional[int]:
        """1-based place in the service order, or None if not queued."""
        items = self._queue._queue
        key = next((k for k, r in items if r.id == request_id), None)
        if key is None:
            return None
        return 1 + sum(1 for other, _ in items if other < key)

    def remove(self, request_id: str) -> Optional[BackendRequestModel]:
        """Pull one request out by id, keeping the heap valid."""
        items = self._queue._queue
        for index, (_, request) in enumerate(items):
            if request.id == request_id:
                items.pop(index)
                heapq.heapify(items)
                return request
        return None

    def clear(self) -> None:
        self._queue._queue.clear()
