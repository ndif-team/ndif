from multiprocessing import Manager
from typing import List, Optional
from .schema import BackendRequestModel
from .logging import load_logger

logger = load_logger(service_name="QueueManager", logger_name="QueueManager")

def get_queue_key(model_key: str) -> str:
    return f"queue:{model_key}"

def get_status_key(model_key: str) -> str:
    return f"model_status:{model_key}"

class QueueManager:
    def __init__(self):
        self.manager = Manager()
        self.queues = self.manager.dict()

    def get_queue(self, model_key: str) -> List[str]:
        """Get all items in the queue for a model key. Returns an empty list if the queue doesn't exist."""
        queue_key = get_queue_key(model_key)
        if queue_key not in self.queues:
            return []
        return list(self.queues[queue_key])

    def get_queue_length(self, model_key: str) -> int:
        """Get the length of the queue for a model key. Returns 0 if queue doesn't exist."""
        queue_key = get_queue_key(model_key)
        if queue_key not in self.queues:
            return 0
        return len(self.queues[queue_key])

    def get_all_model_keys(self) -> List[str]:
        """Get all model keys that have queues."""
        # Get all keys that start with "queue:"
        model_keys = []
        for key in self.queues.keys():
            if key.startswith("queue:"):
                # Remove the "queue:" prefix
                model_keys.append(key[6:])
        return model_keys

    def enqueue(self, request: BackendRequestModel) -> None:
        """Add a request to the queue."""
        queue_key = get_queue_key(request.model_key)
        
        # Initialize queue if it doesn't exist
        if queue_key not in self.queues:
            self.queues[queue_key] = self.manager.list()
        
        # Add to the beginning of the list (LIFO for lpush equivalent)
        queue = self.queues[queue_key]
        queue.insert(0, request)
        self.queues[queue_key] = queue
        
        logger.debug(f"Enqueued request: {request.id}")

    def dequeue(self, model_key: str) -> Optional[BackendRequestModel]:
        """Remove and return the next request from the queue."""
        queue_key = get_queue_key(model_key)
        
        if queue_key not in self.queues or len(self.queues[queue_key]) == 0:
            return None
        
        # Get the last item (FIFO - remove from end)
        queue = self.queues[queue_key]
        model = queue.pop()
        if len(queue) == 0:
            del self.queues[queue_key]
        else:
            self.queues[queue_key] = queue
        
        logger.debug(f"Dequeued request: {model.id}")
        return model