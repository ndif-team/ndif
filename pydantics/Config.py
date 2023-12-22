from typing import Any, Dict, List

from pydantic import BaseModel


class ModelConfigModel(BaseModel):
    repo_id: str
    nnsight_class: str
    kwargs: Dict[str, Any]
    max_memory: Dict[int, str] = None
    checkpoint_path: str = None


class ConfigModel(BaseModel):
    RESPONSE_PATH: str
    PORT: int
    LOG_PATH: str
    ALLOWED_MODULES: List[str]
    DISALLOWED_FUNCTIONS: List[str]
    MODEL_CONFIGURATIONS: List[ModelConfigModel]
