from typing import ClassVar
from pydantic import ConfigDict 
from .mixins import ObjectStorageMixin
from ..providers.objectstore import ObjectStoreProvider
class BackendResultModel(ObjectStorageMixin):
    model_config = ConfigDict(extra='allow', validate_assignment=False, frozen=False, arbitrary_types_allowed=True, str_strip_whitespace=False, strict=False)

    _bucket_name: ClassVar[str] = "dev-ndif-results"
    _file_extension: ClassVar[str] = "pt"
    
    def url(self) -> str:
        return self._url(ObjectStoreProvider.object_store)

