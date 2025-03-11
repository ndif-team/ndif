from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel
from ray.serve.schema import RayActorOptionsSchema
from ..deployments.model import BaseModelDeploymentArgs


class ModelConfigurationSchema(BaseModel):

    
    args: BaseModelDeploymentArgs
    num_replicas: int
    
    model_import_path: Optional[str] = None
    ray_actor_options: Optional[Dict] = {} 



class ServiceConfigurationSchema(BaseModel):

    model_import_path: str
    distributed_model_import_path: str
    request_import_path: str
    request_num_replicas: int

    models: List[ModelConfigurationSchema] = []
