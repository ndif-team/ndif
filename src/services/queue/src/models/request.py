from datetime import datetime, timedelta
from typing import Optional, Callable, Dict, Any
from pydantic import BaseModel, Field

class SocketCallbackMixin:
    """Mixin for socket callback functionality."""
    _socket_callback: Optional[Callable[[str, str, int], None]] = None
    session_id: Optional[str] = None

    def set_socket_callback(self, callback: Callable[[str, str, int], None]) -> None:
        """Set the callback for position updates."""
        self._socket_callback = callback

    def trigger_socket_update(self, session_id: str, request_id: str, position: int) -> None:
        """Trigger socket update if callback exists."""
        if self._socket_callback:
            self._socket_callback(session_id, request_id, position)

class RequestInfo(BaseModel, SocketCallbackMixin):
    """Information about a queued request."""
    request_id: str
    model_key: str
    api_key: Optional[str] = None
    added_at: datetime = Field(default_factory=datetime.now)
    position: int = 0

    def update_position(self, new_position: int) -> None:
        """Update the request's position and trigger socket update if callback exists."""
        self.position = new_position
        if self.session_id:
            self.trigger_socket_update(self.session_id, self.request_id, new_position)

    def time_in_queue(self) -> timedelta:
        """Return the time the request has been in the queue."""
        return datetime.now() - self.added_at 

    def model_dump(self) -> Dict[str, Any]:
        """Get complete state as a dictionary, excluding socket callback."""
        return {
            "request_id": self.request_id,
            "model_key": self.model_key,
            "api_key": self.api_key,
            "position": self.position,
            "added_at": self.added_at
        }