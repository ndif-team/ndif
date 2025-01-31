from typing import ClassVar

from nnsight.schema.result import ResultModel
from .mixins import ObjectStorageMixin

class BackendResultModel(ResultModel, ObjectStorageMixin):

    _bucket_name: ClassVar[str] = "results"
    _file_extension: ClassVar[str] = "pt"