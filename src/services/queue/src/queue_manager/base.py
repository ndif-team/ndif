from abc import ABC, abstractmethod
from collections.abc import MutableMapping
from typing import Iterator, Any, Optional

class BaseQueueManager(MutableMapping, ABC):
    """Abstract base class for queue managers."""
    
    @abstractmethod
    def __getitem__(self, key: str) -> Any:
        """Get the queue for a key."""
        pass
    
    @abstractmethod
    def __setitem__(self, key: str, value: Any) -> None:
        """Set a queue for a key."""
        pass
    
    @abstractmethod
    def __delitem__(self, key: str) -> None:
        """Delete a queue for a key."""
        pass
    
    @abstractmethod
    def __iter__(self) -> Iterator[str]:
        """Iterate over keys."""
        pass
    
    @abstractmethod
    def __len__(self) -> int:
        """Return the number of queues."""
        pass
    
    @abstractmethod
    def enqueue(self, key: str, item: Any) -> None:
        """Add an item to the queue."""
        pass
    
    @abstractmethod
    def dequeue(self, key: str) -> Optional[Any]:
        """Remove and return the next item from the queue."""
        pass 