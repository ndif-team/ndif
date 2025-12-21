from unittest.mock import MagicMock
from common.schema.mixins import ObjectStorageMixin, TelemetryMixin


def test_object_name():
    class MyObject(ObjectStorageMixin):
        _folder_name = "folder"
        _file_extension = "txt"

    assert MyObject.object_name(id="test_id") == "folder/test_id.txt"


def test_backend_log_levels():
    class Dummy(TelemetryMixin):
        pass

    dummy = Dummy()
    logger = MagicMock()
    dummy.backend_log(logger, "INFO: service started", level="info")
    logger.info.assert_called_with("INFO: service started")
    dummy.backend_log(logger, "ERROR: something broke", level="error")
    logger.error.assert_called_with("ERROR: something broke")
    dummy.backend_log(logger, "Exception: there was an exception", level="exception")
    logger.exception.assert_called_with("Exception: there was an exception")
