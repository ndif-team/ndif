from datetime import datetime
from typing import Any, Dict, List, Union

from pydantic import BaseModel


class RequestModel(BaseModel):
    args: List
    kwargs: Dict
    repo_id: str
    batched_input: Union[bytes, Any]
    intervention_graph: Union[bytes, Any]
    generation: bool

    id: str = None
    session_id: str = None
    received: datetime = None
    blocking: bool = False
