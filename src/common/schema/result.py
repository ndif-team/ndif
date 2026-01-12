from typing import ClassVar
from pydantic import ConfigDict
from .mixins import ObjectStorageMixin


class BackendResultModel(ObjectStorageMixin):
    model_config = ConfigDict(
        extra="allow",
        validate_assignment=False,
        frozen=False,
        arbitrary_types_allowed=True,
        str_strip_whitespace=False,
        strict=False,
    )

    _folder_name: ClassVar[str] = "results"
    _file_extension: ClassVar[str] = "pt"
