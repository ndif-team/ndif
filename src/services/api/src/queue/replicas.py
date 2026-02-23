"""Replica state tracking for Processor."""

import asyncio
import logging
import time
from typing import Any, Optional

from ..types import MODEL_KEY, REPLICA_ID
from .util import get_model_actor_handle, submit

logger = logging.getLogger("ndif")


class Replica:
    """Per-replica state and Ray actor handle.

    """

    def __init__(self, model_key: MODEL_KEY, replica_id: REPLICA_ID) -> None:
        self.model_key = model_key
        self.replica_id = replica_id
        self.busy = False
        self.worker_task: Optional[asyncio.Task] = None
        self.current_request_id: Optional[str] = None
        self.current_request_started_at: Optional[float] = None

    def get_handle(self) -> Any:
        """Get the Ray actor handle for this replica."""
        return get_model_actor_handle(self.model_key, self.replica_id)

    async def submit(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Submit an RPC call to this replica's Ray actor."""
        handle = self.get_handle()
        return await submit(handle, method, *args, **kwargs)

    async def wait_until_ready(self, *, poll_interval_s: float = 1.0) -> None:
        """Poll until the Ray actor is ready, retrying on lookup errors."""
        while True:
            try:
                logger.info(f"Checking if replica {self.replica_id} is ready")
                handle = self.get_handle()
                logger.info(f"Handle: {handle}")
                await submit(handle, "__ray_ready__")
                return
            except Exception as exc:
                if str(exc).startswith("Failed to look up actor"):
                    await asyncio.sleep(poll_interval_s)
                    continue
                raise

    def set_current_request(self, request_id: str) -> None:
        self.current_request_id = request_id
        self.current_request_started_at = time.time()

    def clear_current_request(self) -> None:
        self.current_request_id = None
        self.current_request_started_at = None


class Replicas:
    """Collection of Replica objects with in-flight bookkeeping.

    Pure state container — no worker loops, no callbacks, no execute methods.
    """

    def __init__(self, model_key: MODEL_KEY, replica_ids: Optional[list[REPLICA_ID]] = None) -> None:
        self.model_key = model_key
        replica_ids = list(dict.fromkeys(replica_ids or []))
        self.replica_ids = replica_ids
        self.replica_count = len(replica_ids)
        self.in_flight = 0
        self._replicas: dict[REPLICA_ID, Replica] = {
            rid: Replica(model_key, rid) for rid in self.replica_ids
        }

    @property
    def current_request_ids(self) -> dict[REPLICA_ID, Optional[str]]:
        return {
            rid: r.current_request_id for rid, r in self._replicas.items()
        }

    @property
    def current_request_started_ats(self) -> dict[REPLICA_ID, Optional[float]]:
        return {
            rid: r.current_request_started_at for rid, r in self._replicas.items()
        }

    @property
    def worker_tasks(self) -> dict[REPLICA_ID, asyncio.Task]:
        return {
            rid: r.worker_task
            for rid, r in self._replicas.items()
            if r.worker_task is not None
        }

    def get_state(self) -> dict[str, Any]:
        return {
            "current_request_ids": self.current_request_ids,
            "current_request_started_ats": self.current_request_started_ats,
            "in_flight": self.in_flight,
            "replica_count": self.replica_count,
            "replica_ids": self.replica_ids,
        }

    def has(self, replica_id: REPLICA_ID) -> bool:
        return replica_id in self._replicas

    def get(self, replica_id: REPLICA_ID) -> Replica:
        return self._replicas[replica_id]

    def add(self, replica_id: REPLICA_ID) -> bool:
        if replica_id in self._replicas:
            return False
        self.replica_ids.append(replica_id)
        self.replica_count = len(self.replica_ids)
        self._replicas[replica_id] = Replica(self.model_key, replica_id)
        return True

    def remove(self, replica_id: REPLICA_ID) -> bool:
        if replica_id not in self._replicas:
            return False
        self.replica_ids.remove(replica_id)
        self.replica_count = len(self.replica_ids)
        replica = self._replicas.pop(replica_id, None)
        if (
            replica is not None
            and replica.worker_task is not None
            and not replica.worker_task.done()
        ):
            replica.worker_task.cancel()
        return True

    def set_replica_ids(self, replica_ids: list[REPLICA_ID]) -> None:
        normalized = list(dict.fromkeys(replica_ids))
        normalized_set = set(normalized)

        for existing_id in list(self._replicas.keys()):
            if existing_id not in normalized_set:
                self.remove(existing_id)

        for rid in normalized:
            if rid not in self._replicas:
                self._replicas[rid] = Replica(self.model_key, rid)

        self.replica_ids = normalized
        self.replica_count = len(normalized)

    def begin_request(self, replica_id: REPLICA_ID) -> None:
        self.in_flight += 1
        self._replicas[replica_id].busy = True

    def end_request(self, replica_id: REPLICA_ID) -> None:
        self.in_flight -= 1
        self._replicas[replica_id].busy = False
