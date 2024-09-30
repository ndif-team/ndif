from typing import ClassVar

from nnsight.schema.Response import ResultModel
from .mixins import ObjectStorageMixin

class BackendResultModel(ResultModel, ObjectStorageMixin):
    """
    A model representing backend results, inheriting from ResultModel and ObjectStorageMixin.

    This class combines the functionality of ResultModel for handling result data
    and ObjectStorageMixin for object storage operations. It is specifically configured
    to store results in a 'results' bucket with a '.pt' file extension, typically used
    for PyTorch serialized objects.

    Attributes:
        _bucket_name (ClassVar[str]): The name of the bucket where results are stored.
        _file_extension (ClassVar[str]): The file extension used for stored result files.
    """

    _bucket_name: ClassVar[str] = "results"
    _file_extension: ClassVar[str] = "pt"