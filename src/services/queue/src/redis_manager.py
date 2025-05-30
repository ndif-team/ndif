import redis
from typing import List, Optional
import ray
import base64
import zlib

from .schema import BackendRequestModel
from .logging import load_logger

logger = load_logger(service_name="RedisManager", logger_name="RedisManager")

def get_queue_key(model_key: str) -> str:
    return f"queue:{model_key}"

def get_status_key(model_key: str) -> str:
    return f"model_status:{model_key}"

class RedisQueueManager:
    def __init__(self, redis_host: str, redis_port: int, redis_db: int):
        self.redis = redis.Redis(host=redis_host, port=redis_port, db=redis_db, decode_responses=True)

    def get_queue(self, model_key: str) -> List[str]:
        """Get all items in the queue for a model key. Returns an empty list if the queue doesn't exist."""
        queue_key = get_queue_key(model_key)
        return self.redis.lrange(queue_key, 0, -1)

    def get_queue_length(self, model_key: str) -> int:
        """Get the length of the queue for a model key. Returns 0 if queue doesn't exist."""
        queue_key = get_queue_key(model_key)
        return self.redis.llen(queue_key)

    def get_all_model_keys(self) -> List[str]:
        """Get all model keys that have queues."""
        pattern = "queue:*"
        keys = self.redis.keys(pattern)
        # Remove the "queue:" prefix from each key
        return [key[6:] for key in keys]

    def enqueue(self, request: BackendRequestModel) -> None:
        """Add a request to the queue."""
        queue_key = get_queue_key(request.model_key)
        
        # Convert graph to base64 for JSON serialization
        if isinstance(request.graph, bytes):
            request.graph = base64.b64encode(request.graph).decode('utf-8')
        
        # Convert pydantic model to JSON string
        request_json = request.model_dump_json()
        self.redis.lpush(queue_key, request_json)

    def dequeue(self, model_key: str) -> Optional[BackendRequestModel]:
        """Remove and return the next request from the queue."""
        queue_key = get_queue_key(model_key)
        
        # Get the last item (FIFO)
        result_json = self.redis.rpop(queue_key)
        if not result_json:
            return None
        
        try:
            # Parse the model
            model = BackendRequestModel.model_validate_json(result_json)
            
            # Convert base64 graph back to bytes if it's a string
            if isinstance(model.graph, str):
                model.graph = base64.b64decode(model.graph.encode('utf-8'))
                
            return model
        except Exception as e:
            logger.error(f"Error parsing dequeued item: {e}")
            return None

    # def set_status(self, model_key: str, status: str) -> None:
    #     """Set the status for a model key."""
    #     status_key = get_status_key(model_key)
    #     self.redis.set(status_key, status)
    
    # def get_status(self, model_key: str) -> Optional[str]:
    #     """Get the status for a model key."""
    #     status_key = get_status_key(model_key)
    #     return self.redis.get(status_key)
    
    # def delete_request(self, model_key: str, request_id: str) -> int:
    #     """Delete a specific request from the queue by request_id. Returns number of items removed."""
    #     queue_key = get_queue_key(model_key)
        
    #     # Get all items in the queue
    #     items = self.redis.lrange(queue_key, 0, -1)
    #     removed_count = 0
        
    #     # Remove items that match the request_id
    #     for item in items:
    #         try:
    #             # Parse the JSON dumped pydantic model
    #             item_data = json.loads(item)
    #             if isinstance(item_data, dict) and item_data.get('id') == request_id:
    #                 self.redis.lrem(queue_key, 1, item)
    #                 removed_count += 1
    #         except (json.JSONDecodeError, AttributeError):
    #             # If item is not valid JSON or doesn't have the expected structure, skip
    #             continue
        
    #     # If queue is now empty, delete it
    #     if self.redis.llen(queue_key) == 0:
    #         self.redis.delete(queue_key)
        
    #     return removed_count