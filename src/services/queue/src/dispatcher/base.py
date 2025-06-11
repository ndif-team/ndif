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
        """Start the dispatcher loop."""
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self.dispatch_loop())
    
    async def stop(self):
        """Stop the dispatcher loop."""
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
        """Main loop to dispatch requests."""
        while self.running:
            try:
                await self.check_and_dispatch()
            except Exception:
                pass
            finally:
                await asyncio.sleep(self.poll_interval)
    
    async def check_and_dispatch(self):
        """Check all task sources and dispatch available tasks."""
        task_sources = await self.get_task_sources()
        for task_source in task_sources:
            if await self.has_pending_tasks(task_source):
                batch = await self.get_next_batch(task_source)
                if not batch:
                    continue
                await self.dispatch_batch(batch)
    
    @abstractmethod
    async def get_task_sources(self) -> List[str]:
        """Get all task sources that should be checked for dispatching."""
        pass
    
    @abstractmethod
    async def has_pending_tasks(self, task_source: str) -> bool:
        """Check if there are pending tasks for the given task source."""
        pass
    
    @abstractmethod
    async def get_next_batch(self, task_source: str) -> List[Any]:
        """Get the next batch of tasks to dispatch for the given task source."""
        pass
    
    @abstractmethod
    async def dispatch_batch(self, batch: List[Any]):
        """Dispatch a batch of tasks."""
        pass 