from abc import ABC, abstractmethod
from collections.abc import MutableMapping
from typing import Iterator, Any, Optional

class BaseQueueManager(MutableMapping, ABC):
    """Abstract base class for queue managers."""
    @abstractmethod
    def __getitem__(self, key: str) -> Any:
        pass
    @abstractmethod
    def __setitem__(self, key: str, value: Any) -> None:
        pass
    @abstractmethod
    def __delitem__(self, key: str) -> None:
        pass
    @abstractmethod
    def __iter__(self) -> Iterator[str]:
        pass
    @abstractmethod
    def __len__(self) -> int:
        pass
    @abstractmethod
    def enqueue(self, key: str, item: Any) -> None:
        pass
    @abstractmethod
    def dequeue(self, key: str) -> Optional[Any]:
        pass 