"""Replica state tracking for Processor."""

import asyncio
import time
from typing import Any, Awaitable, Callable, Coroutine, Optional


class Replica:
    """Per-replica state and worker lifecycle."""

    def __init__(self, replica_id: int) -> None:
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

    def start_worker(self, worker_coro_factory: Callable[[int], Coroutine]) -> bool:
        if self.worker_task is not None and not self.worker_task.done():
            return False
        self.worker_task = asyncio.create_task(worker_coro_factory(self.replica_id))
        return True

    async def wait_until_ready(
        self,
        get_handle: Callable[[int], Any],
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
        get_handle: Callable[[int], Any],
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

    def __init__(self, replica_count: int) -> None:
        self.replica_ids = list(range(replica_count))
        self.replica_count = replica_count
        self.in_flight = 0
        self._replicas: dict[int, Replica] = {
            replica_id: Replica(replica_id) for replica_id in self.replica_ids
        }

    @property
    def current_request_ids(self) -> dict[int, Optional[str]]:
        return {
            replica_id: replica.current_request_id
            for replica_id, replica in self._replicas.items()
        }

    @property
    def current_request_started_ats(self) -> dict[int, Optional[float]]:
        return {
            replica_id: replica.current_request_started_at
            for replica_id, replica in self._replicas.items()
        }

    @property
    def worker_tasks(self) -> dict[int, asyncio.Task]:
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

    def has(self, replica_id: int) -> bool:
        return replica_id in self._replicas

    def add(self, replica_id: int) -> bool:
        if replica_id in self._replicas:
            return False
        self.replica_ids.append(replica_id)
        self.replica_ids.sort()
        self.replica_count = len(self.replica_ids)
        self._replicas[replica_id] = Replica(replica_id)
        return True

    def remove(self, replica_id: int) -> bool:
        if replica_id not in self._replicas:
            return False
        self.replica_ids.remove(replica_id)
        self.replica_count = len(self.replica_ids)
        self._replicas.pop(replica_id, None)
        return True

    def start_worker(
        self,
        replica_id: int,
        *,
        should_stop: Callable[[], bool],
        get_request: Callable[[], Awaitable[Any]],
        execute_request: Callable[[int, Any], Awaitable[None]],
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
        replica_id: int,
        get_handle: Callable[[int], Any],
        submit_fn: Callable[..., Awaitable[Any]],
        *,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        await self._replicas[replica_id].wait_until_ready(
            get_handle, submit_fn, log_fn=log_fn
        )

    async def execute_request(
        self,
        replica_id: int,
        request: Any,
        get_handle: Callable[[int], Any],
        submit_fn: Callable[..., Awaitable[Any]],
    ) -> None:
        await self._replicas[replica_id].execute_request(request, get_handle, submit_fn)

    async def _run_worker_loop(
        self,
        replica_id: int,
        *,
        should_stop: Callable[[], bool],
        get_request: Callable[[], Awaitable[Any]],
        execute_request: Callable[[int, Any], Awaitable[None]],
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

    def begin_request(self, replica_id: int) -> None:
        self.in_flight += 1
        self._replicas[replica_id].busy = True

    def end_request(self, replica_id: int) -> None:
        self.in_flight -= 1
        self._replicas[replica_id].busy = False
