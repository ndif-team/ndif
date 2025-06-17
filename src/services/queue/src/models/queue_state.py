from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Callable
from pydantic import BaseModel, Field
from .request import RequestInfo
from .model_state import ModelState

class QueueState(BaseModel):
    """Complete state of the queue system."""
    model_states: Dict[str, ModelState] = Field(default_factory=dict)
    _socket_callback: Optional[Callable[[str, str, int], None]] = None

    def set_socket_callback(self, callback: Callable[[str, str, int], None]) -> None:
        """Set the callback for position updates."""
        self._socket_callback = callback
        # Update callback for all existing requests
        for state in self.model_states.values():
            for request in state.queued_requests.values():
                request.set_socket_callback(callback)

    def __getitem__(self, model_key: str) -> ModelState:
        """Get or create model state for a model key."""
        if model_key not in self.model_states:
            self.model_states[model_key] = ModelState(
                model_key=model_key,
                socket_callback=self._socket_callback
            )
        return self.model_states[model_key]

    def __setitem__(self, model_key: str, state: ModelState) -> None:
        """Set model state for a key."""
        self.model_states[model_key] = state

    def __delitem__(self, model_key: str) -> None:
        """Delete model state for a key."""
        del self.model_states[model_key]

    def __contains__(self, model_key: str) -> bool:
        """Check if a model key exists."""
        return model_key in self.model_states

    def __iter__(self):
        """Iterate over model keys."""
        return iter(self.model_states)

    def __len__(self) -> int:
        """Return number of model states."""
        return len(self.model_states)

    def keys(self):
        """Get all model keys."""
        return self.model_states.keys()

    def values(self):
        """Get all model states."""
        return self.model_states.values()

    def items(self):
        """Get all model key-state pairs."""
        return self.model_states.items()

    @property
    def active_models(self) -> Set[str]:
        """Get all active models."""
        return {mk for mk, state in self.model_states.items() if state.is_active}

    @property  
    def inactive_models(self) -> Set[str]:
        """Get all inactive models."""
        return {mk for mk, state in self.model_states.items() if not state.is_active}

    def model_dump(self) -> Dict[str, Any]:
        """Get complete state as a dictionary."""
        return {
            "model_states": {
                model_key: state.model_dump()
                for model_key, state in self.model_states.items()
            },
            "active_models": list(self.active_models),
            "inactive_models": list(self.inactive_models)
        }

    def get_model_keys(self) -> List[str]:
        """Get all model keys that have requests."""
        return list(self.model_states.keys())

    def get_model_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all models."""
        return {
            model_key: state.get_stats()
            for model_key, state in self.model_states.items()
        }

    def add_request(self, request_id: str, model_key: str, api_key: Optional[str] = None, 
                   session_id: Optional[str] = None) -> None:
        """Add a new request to the tracking system."""
        if model_key not in self.model_states:
            self.model_states[model_key] = ModelState(
                model_key=model_key,
                socket_callback=self._socket_callback
            )
        self[model_key].create_request(request_id, api_key, session_id)
        

    def remove_request(self, request_id: str) -> None:
        """Remove a request from the tracking system."""
        # Find the model key for this request
        model_key = None
        for mk, state in self.model_states.items():
            if request_id in state.queued_requests:
                model_key = mk
                break
        
        if model_key is None:
            return
        
        state = self[model_key]
        request_info = state.queued_requests.get(request_id)
        
        if request_info is None:
            return
        
        state.remove_request(request_id)


    def get_request_position(self, request_id: str) -> Optional[int]:
        """Get the current position of a request in its model's queue."""
        for state in self.model_states.values():
            if request_id in state.queued_requests:
                return state.queued_requests[request_id].position
        return None

    def get_model_requests(self, model_key: str) -> List[Dict[str, Any]]:
        """Get all requests for a model key with their details."""
        state = self[model_key]
        return [req_info.dict() for req_info in state.queued_requests.values()]

    def request_exists(self, request_id: str) -> bool:
        """Check if a request exists in the tracking system."""
        return any(
            request_id in state.queued_requests or request_id in state.dispatched_requests
            for state in self.model_states.values()
        )

    def model_key_exists(self, model_key: str) -> bool:
        """Check if a model key has any requests in the queue."""
        state = self[model_key]
        return bool(state.queued_requests or state.dispatched_requests)