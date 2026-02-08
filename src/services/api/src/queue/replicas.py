"""Replica state tracking for Processor."""

import asyncio
import time
from typing import Any, Awaitable, Callable, Coroutine, Optional

from ..types import REPLICA_ID


class Replica:
    """Per-replica state and worker lifecycle."""

    def __init__(self, replica_id: REPLICA_ID) -> None:
        self.replica_id = replica_id
        self.busy = False
        self.worker_task: Optional[asyncio.Task] = None
        self.current_request_id: Optional[str] = None
        self.current_request_started_at: Optional[float] = None

    def set_current_request(self, request_id: str) -> None:
        self.current_request_id = request_id
        self.current_request_started_at = time.time()

    def clear_current_request(self) -> None:
        self.current_request_id = None
        self.current_request_started_at = None

    def start_worker(
        self, worker_coro_factory: Callable[[REPLICA_ID], Coroutine]
    ) -> bool:
        if self.worker_task is not None and not self.worker_task.done():
            return False
        self.worker_task = asyncio.create_task(worker_coro_factory(self.replica_id))
        return True

    async def wait_until_ready(
        self,
        get_handle: Callable[[REPLICA_ID], Any],
        submit_fn: Callable[..., Awaitable[Any]],
        *,
        log_fn: Optional[Callable[[str], None]] = None,
        lookup_error_prefix: str = "Failed to look up actor",
        poll_interval_s: float = 1.0,
    ) -> None:
        while True:
            try:
                if log_fn is not None:
                    log_fn(f"Checking if replica {self.replica_id} is ready")
                handle = get_handle(self.replica_id)
                if log_fn is not None:
                    log_fn(f"Handle: {handle}")
                await submit_fn(handle, "__ray_ready__")
                return
            except Exception as exc:
                if str(exc).startswith(lookup_error_prefix):
                    await asyncio.sleep(poll_interval_s)
                    continue
                raise

    async def execute_request(
        self,
        request: Any,
        get_handle: Callable[[REPLICA_ID], Any],
        submit_fn: Callable[..., Awaitable[Any]],
    ) -> None:
        self.set_current_request(request.id)
        try:
            handle = get_handle(self.replica_id)
            await submit_fn(handle, "__call__", request)
        finally:
            self.clear_current_request()


class Replicas:
    """Track replica state and per-replica request metadata."""

    def __init__(self, replica_ids: Optional[list[REPLICA_ID]] = None) -> None:
        replica_ids = list(dict.fromkeys(replica_ids or []))
        self.replica_ids = replica_ids
        self.replica_count = len(replica_ids)
        self.in_flight = 0
        self._replicas: dict[REPLICA_ID, Replica] = {
            replica_id: Replica(replica_id) for replica_id in self.replica_ids
        }

    @property
    def current_request_ids(self) -> dict[REPLICA_ID, Optional[str]]:
        return {
            replica_id: replica.current_request_id
            for replica_id, replica in self._replicas.items()
        }

    @property
    def current_request_started_ats(self) -> dict[REPLICA_ID, Optional[float]]:
        return {
            replica_id: replica.current_request_started_at
            for replica_id, replica in self._replicas.items()
        }

    @property
    def worker_tasks(self) -> dict[REPLICA_ID, asyncio.Task]:
        return {
            replica_id: replica.worker_task
            for replica_id, replica in self._replicas.items()
            if replica.worker_task is not None
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

    def add(self, replica_id: REPLICA_ID) -> bool:
        if replica_id in self._replicas:
            return False
        self.replica_ids.append(replica_id)
        self.replica_count = len(self.replica_ids)
        self._replicas[replica_id] = Replica(replica_id)
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
        normalized_replica_ids = list(dict.fromkeys(replica_ids))
        normalized_replica_id_set = set(normalized_replica_ids)

        for existing_replica_id in list(self._replicas.keys()):
            if existing_replica_id not in normalized_replica_id_set:
                self.remove(existing_replica_id)

        for replica_id in normalized_replica_ids:
            if replica_id not in self._replicas:
                self._replicas[replica_id] = Replica(replica_id)

        self.replica_ids = normalized_replica_ids
        self.replica_count = len(self.replica_ids)

    def start_worker(
        self,
        replica_id: REPLICA_ID,
        *,
        should_stop: Callable[[], bool],
        get_request: Callable[[], Awaitable[Any]],
        execute_request: Callable[[REPLICA_ID, Any], Awaitable[None]],
        on_busy: Callable[[], None],
        on_idle: Callable[[], None],
        on_before_execute: Optional[Callable[[], None]] = None,
    ) -> bool:
        return self._replicas[replica_id].start_worker(
            lambda rid: self._run_worker_loop(
                rid,
                should_stop=should_stop,
                get_request=get_request,
                execute_request=execute_request,
                on_busy=on_busy,
                on_idle=on_idle,
                on_before_execute=on_before_execute,
            )
        )

    async def wait_until_ready(
        self,
        replica_id: REPLICA_ID,
        get_handle: Callable[[REPLICA_ID], Any],
        submit_fn: Callable[..., Awaitable[Any]],
        *,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        await self._replicas[replica_id].wait_until_ready(
            get_handle, submit_fn, log_fn=log_fn
        )

    async def execute_request(
        self,
        replica_id: REPLICA_ID,
        request: Any,
        get_handle: Callable[[REPLICA_ID], Any],
        submit_fn: Callable[..., Awaitable[Any]],
    ) -> None:
        await self._replicas[replica_id].execute_request(request, get_handle, submit_fn)

    async def _run_worker_loop(
        self,
        replica_id: REPLICA_ID,
        *,
        should_stop: Callable[[], bool],
        get_request: Callable[[], Awaitable[Any]],
        execute_request: Callable[[REPLICA_ID, Any], Awaitable[None]],
        on_busy: Callable[[], None],
        on_idle: Callable[[], None],
        on_before_execute: Optional[Callable[[], None]] = None,
    ) -> None:
        while not should_stop():
            if not self.has(replica_id):
                return

            request = await get_request()
            if on_before_execute is not None:
                on_before_execute()

            self.begin_request(replica_id)
            on_busy()
            try:
                await execute_request(replica_id, request)
            finally:
                self.end_request(replica_id)
                if self.in_flight == 0:
                    on_idle()

    def begin_request(self, replica_id: REPLICA_ID) -> None:
        self.in_flight += 1
        self._replicas[replica_id].busy = True

    def end_request(self, replica_id: REPLICA_ID) -> None:
        self.in_flight -= 1
        self._replicas[replica_id].busy = False
