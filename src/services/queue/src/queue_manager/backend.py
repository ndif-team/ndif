import queue
from multiprocessing import Manager
from typing import Optional, Any
from ..schema import BackendRequestModel
from ..logging import load_logger
from .base import BaseQueueManager
from ..models.queue_state import QueueState

logger = load_logger(service_name="QueueManager", logger_name="QueueManager")

class BackendQueueManager(BaseQueueManager):
    """Queue manager for backend requests using multiprocessing-safe queues."""
    
    def __init__(self, queue_state: Optional[QueueState] = None, delete_on_empty: bool = True):
        super().__init__(delete_on_empty)
        self.manager = Manager()
        self.queues = self.manager.dict()
        self.state = queue_state or QueueState()
    
    def __getitem__(self, key: str) -> queue.Queue:
        """Get the queue for a key."""
        if key not in self.queues:
            raise KeyError(f"No queue found for key: {key}")
        return self.queues[key]
    
    # TODO: Have this update the state
    def __setitem__(self, key: str, value: Any) -> None:
        """Set a queue for a key."""
        if key not in self.queues:
            self.queues[key] = self.manager.Queue()
        
        q = self.queues[key]
        if isinstance(value, (list, tuple)):
            for item in value:
                q.put(item)
        else:
            q.put(value)
    
    # TODO: Have this update the state
    def __delitem__(self, key: str) -> None:
        """Delete a queue for a key."""
        if key in self.queues:
            del self.queues[key]
        else:
            raise KeyError(f"No queue found for key: {key}")
    
    def __iter__(self):
        """Iterate over keys."""
        return iter(self.queues.keys())
    
    def __len__(self) -> int:
        """Return the number of queues."""
        return len(self.queues)
    
    def __contains__(self, key: str) -> bool:
        """Check if a key has a queue."""
        return key in self.queues
    
    def enqueue(self, request: BackendRequestModel) -> None:
        """Add a request to the queue."""
        key = request.model_key
        if key not in self.queues:
            self.queues[key] = self.manager.Queue()
        
        self.queues[key].put(request)
        
        self.state[key].create_request(request.id, request.api_key, request.session_id)
        logger.debug(f"Enqueued request: {request.id}")
    
    def dequeue(self, key: str) -> Optional[BackendRequestModel]:
        """Remove and return the next request from the queue."""
        if key not in self.queues:
            return None
        
        q = self.queues[key]
        try:
            item = q.get_nowait()
            self.state[key].dispatch_request(item.id)
            logger.debug(f"Dequeued request: {item.id}")
            return item
        except queue.Empty:
            return None

    def _delete_if_empty(self, key: str) -> None:
        """Delete a queue if it is empty."""
        if key in self.queues:
            q = self.queues[key]
            try:
                # Try to peek at the queue without removing anything
                item = q.get_nowait()
                # If we get here, the queue wasn't empty, so put the item back
                q.put(item)
            except queue.Empty:
                # Queue is empty, safe to delete
                del self.queues[key]
                logger.debug(f"Deleted empty queue for key: {key}")

# For backward compatibility
QueueManager = BackendQueueManager 