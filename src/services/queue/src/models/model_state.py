from datetime import datetime
from typing import Dict, List, Optional, Any, Callable
from pydantic import BaseModel, Field
from .request import RequestInfo

class ModelState(BaseModel):
    """State information for a specific model."""
    model_key: str
    last_dispatch: Optional[datetime] = None
    queued_requests: Dict[str, RequestInfo] = Field(default_factory=dict)
    dispatched_requests: Dict[str, RequestInfo] = Field(default_factory=dict)
    _socket_callback: Optional[Callable[[str, str, int], None]] = None

    def set_socket_callback(self, callback: Callable[[str, str, int], None]) -> None:
        """Set the callback for position updates."""
        self._socket_callback = callback
        for request in self.queued_requests.values():
            request.set_socket_callback(callback)

    @property
    def is_active(self) -> bool:
        """Return True if the model is active."""
        return bool(self.queued_requests or self.dispatched_requests)

    def __len__(self) -> int:
        """Return the number of requests pending in the queue."""
        return len(self.queued_requests)

    def create_request(self, request_id: str, api_key: Optional[str] = None, 
                      session_id: Optional[str] = None) -> RequestInfo:
        """Create a new request and add it to the queue."""
        position = len(self.queued_requests)
        request_info = RequestInfo(
            request_id=request_id,
            model_key=self.model_key,
            api_key=api_key,
            session_id=session_id,
            position=position
        )
        if self._socket_callback:
            request_info.set_socket_callback(self._socket_callback)
        self.queued_requests[request_id] = request_info
        return request_info

    def remove_request(self, request_id: str) -> None:
        """Remove a request from either queued or dispatched state."""
        if request_id in self.queued_requests:
            del self.queued_requests[request_id]
            # Update positions for remaining requests
            for i, req_info in enumerate(self.queued_requests.values()):
                req_info.update_position(i)
        elif request_id in self.dispatched_requests:
            del self.dispatched_requests[request_id]

    def dispatch_request(self, request_id: str) -> Optional[RequestInfo]:
        """Move a request from queued to dispatched state."""
        if request_id not in self.queued_requests:
            return None
            
        request_info = self.queued_requests.pop(request_id)
        self.dispatched_requests[request_id] = request_info
        self.last_dispatch = datetime.now()
        
        # Update positions for remaining requests
        for i, req_info in enumerate(self.queued_requests.values()):
            req_info.update_position(i)
            
        return request_info

    def clear_dispatched(self) -> None:
        """Clear all dispatched requests."""
        self.dispatched_requests.clear()

    def model_dump(self) -> Dict[str, Any]:
        """Get complete state as a dictionary."""
        return {
            "model_key": self.model_key,
            "last_dispatch": self.last_dispatch,
            "is_active": self.is_active,
            "queued_requests": {
                req_id: req.model_dump()
                for req_id, req in self.queued_requests.items()
            },
            "dispatched_requests": {
                req_id: req.model_dump()
                for req_id, req in self.dispatched_requests.items()
            }
        }