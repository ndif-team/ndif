from datetime import datetime
from typing import Dict, List, Optional
from pydantic import BaseModel, Field

class RequestInfo(BaseModel):
    """Information about a queued request."""
    model_key: str
    api_key: str
    added_at: datetime = Field(default_factory=datetime.now)
    position: int

class ModelQueueStats(BaseModel):
    """Statistics for a model's queue."""
    queue_length: int
    last_dispatch: Optional[datetime] = None

class QueueStats(BaseModel):
    """Overall queue statistics."""
    total_requests: int
    model_keys: Dict[str, ModelQueueStats]
    api_keys: Dict[str, int]

class QueueState(BaseModel):
    """Complete state of the queue system."""
    last_dispatch: Dict[str, datetime] = Field(default_factory=dict)
    requests: Dict[str, RequestInfo] = Field(default_factory=dict)
    api_key_requests: Dict[str, List[str]] = Field(default_factory=dict)
    model_requests: Dict[str, List[str]] = Field(default_factory=dict) 