from datetime import datetime
from typing import Dict, List, Optional, Any
from ..models.state import QueueState, RequestInfo, QueueStats, ModelQueueStats
from ..logging import load_logger

logger = load_logger(service_name="QueueState", logger_name="QueueState")

class QueueStateManager:
    """Manages the state of the queue system using Pydantic models."""
    
    def __init__(self):
        self._state = QueueState()
    
    def update_dispatch(self, model_key: str) -> None:
        """Update the last dispatch time for a model key."""
        self._state.last_dispatch[model_key] = datetime.now()
        logger.debug(f"Updated dispatch time for model key: {model_key}")
    
    def get_last_dispatch(self, model_key: str) -> Optional[datetime]:
        """Get the last dispatch time for a model key."""
        return self._state.last_dispatch.get(model_key)
    
    def get_all_dispatches(self) -> Dict[str, datetime]:
        """Get all model keys and their last dispatch times."""
        return self._state.last_dispatch.copy()
    
    def add_request(self, request_id: str, model_key: str, api_key: Optional[str] = None) -> None:
        """Add a new request to the tracking system."""
        request_info = RequestInfo(
            model_key=model_key,
            api_key=api_key,
            position=len(self._state.model_requests.get(model_key, []))
        )
        
        self._state.requests[request_id] = request_info
        
        if api_key not in self._state.api_key_requests:
            self._state.api_key_requests[api_key] = []
        self._state.api_key_requests[api_key].append(request_id)
        
        if model_key not in self._state.model_requests:
            self._state.model_requests[model_key] = []
        self._state.model_requests[model_key].append(request_id)
        
        logger.debug(f"Added request {request_id} to tracking system")
    
    def remove_request(self, request_id: str) -> None:
        """Remove a request from the tracking system and update positions."""
        if request_id not in self._state.requests:
            return
        
        request_info = self._state.requests[request_id]
        model_key = request_info.model_key
        api_key = request_info.api_key
        
        # Remove from all tracking collections
        del self._state.requests[request_id]
        
        if api_key in self._state.api_key_requests:
            self._state.api_key_requests[api_key].remove(request_id)
        
        if model_key in self._state.model_requests:
            self._state.model_requests[model_key].remove(request_id)
            # Update positions for all remaining requests
            for i, req_id in enumerate(self._state.model_requests[model_key]):
                if req_id in self._state.requests:
                    self._state.requests[req_id].position = i
        
        logger.debug(f"Removed request {request_id} from tracking system")
    
    def get_request_position(self, request_id: str) -> Optional[int]:
        """Get the current position of a request in its model's queue."""
        if request_id not in self._state.requests:
            return None
        return self._state.requests[request_id].position
    
    def get_model_queue_length(self, model_key: str) -> int:
        """Get the current length of a model's queue."""
        return len(self._state.model_requests.get(model_key, []))
    
    def get_api_key_request_count(self, api_key: str) -> int:
        """Get the number of requests for an API key."""
        return len(self._state.api_key_requests.get(api_key, []))
    
    def get_api_key_requests(self, api_key: str) -> List[Dict[str, Any]]:
        """Get all requests for an API key with their details."""
        return [
            {**self._state.requests[req_id].dict(), 'request_id': req_id}
            for req_id in self._state.api_key_requests.get(api_key, [])
            if req_id in self._state.requests
        ]
    
    def get_model_requests(self, model_key: str) -> List[Dict[str, Any]]:
        """Get all requests for a model key with their details."""
        return [
            {**self._state.requests[req_id].dict(), 'request_id': req_id}
            for req_id in self._state.model_requests.get(model_key, [])
            if req_id in self._state.requests
        ]
    
    def request_exists(self, request_id: str) -> bool:
        """Check if a request exists in the tracking system."""
        return request_id in self._state.requests
    
    def model_key_exists(self, model_key: str) -> bool:
        """Check if a model key has any requests in the queue."""
        return bool(self._state.model_requests.get(model_key))
    
    def get_all_model_keys(self) -> List[str]:
        """Get all model keys that have requests in the queue."""
        return list(self._state.model_requests.keys())
    
    def get_queue_stats(self) -> QueueStats:
        """Get overall queue statistics."""
        return QueueStats(
            total_requests=len(self._state.requests),
            model_keys={
                model_key: ModelQueueStats(
                    queue_length=len(requests),
                    last_dispatch=self._state.last_dispatch.get(model_key)
                )
                for model_key, requests in self._state.model_requests.items()
            },
            api_keys={
                api_key: len(requests)
                for api_key, requests in self._state.api_key_requests.items()
            }
        ) 