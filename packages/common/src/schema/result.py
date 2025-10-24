from typing import ClassVar
from pydantic import ConfigDict
from .mixins import ObjectStorageMixin

class BackendResultModel(ObjectStorageMixin):
    
    model_config = ConfigDict(extra='allow')

    _bucket_name: ClassVar[str] = "dev-ndif-results"
    _file_extension: ClassVar[str] = "pt"
