import logging
from common.logging.logger import CustomJSONFormatter, RetryingLokiHandler
import pytest
from unittest.mock import patch


@pytest.fixture
def log_record():
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=123,
        msg="hello",
        args=(),
        exc_info=None,
    )


def test_custom_json_formatter(log_record):
    formatter = CustomJSONFormatter(
        "api",
        fmt="%(service_name)s %(hostname)s %(process_id)d %(thread_name)s %(message)s",
    )
    formatted_record = formatter.format(log_record)
    assert "api" in formatted_record
    assert "hello" in formatted_record


def test_loki_handler_retries_then_succeeds(log_record):
    handler = RetryingLokiHandler(retry_count=3, url="http://localhost:3100")
    emit_exceptions = [Exception("fail"), Exception("fail"), None]
    with (
        patch(
            "logging_loki.LokiHandler.emit", side_effect=emit_exceptions
        ) as mock_emit,
        patch("time.sleep") as mock_sleep,
    ):
        handler.emit(log_record)
        assert mock_emit.call_count == 3
        mock_sleep.assert_called()
