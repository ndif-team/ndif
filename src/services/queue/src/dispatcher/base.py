import asyncio
from abc import ABC, abstractmethod
from typing import List, Any

class BaseDispatcher(ABC):
    """Abstract base class for dispatchers."""
    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self.running = False
        self._task = None
    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self.dispatch_loop())
    async def stop(self):
        if not self.running:
            return
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    async def dispatch_loop(self):
        while self.running:
            try:
                await self.check_and_dispatch()
            except Exception:
                pass
            finally:
                await asyncio.sleep(self.poll_interval)
    async def check_and_dispatch(self):
        task_sources = await self.get_task_sources()
        for task_source in task_sources:
            if await self.has_pending_tasks(task_source):
                batch = await self.get_next_batch(task_source)
                if not batch:
                    continue
                await self.dispatch_batch(batch)
    @abstractmethod
    async def get_task_sources(self) -> List[str]:
        pass
    @abstractmethod
    async def has_pending_tasks(self, task_source: str) -> bool:
        pass
    @abstractmethod
    async def get_next_batch(self, task_source: str) -> List[Any]:
        pass
    @abstractmethod
    async def dispatch_batch(self, batch: List[Any]):
        pass 