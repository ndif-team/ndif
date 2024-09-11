from typing import ClassVar

from nnsight.schema.Response import ResultModel
from .mixins import ObjectStorageMixin

class BackendResultModel(ResultModel, ObjectStorageMixin):

    _bucket_name: ClassVar[str] = "results"
    _file_extension: ClassVar[str] = "pt"