from unittest.mock import MagicMock, patch
from influxdb_client import Point
from common.metrics import Metric


def test_update_creates_client_and_writes(monkeypatch):
    monkeypatch.setenv("INFLUXDB_ADDRESS", "http://localhost:8086")
    monkeypatch.setenv("INFLUXDB_ADMIN_TOKEN", "token")
    monkeypatch.setenv("INFLUXDB_BUCKET", "bucket")
    monkeypatch.setenv("INFLUXDB_ORG", "org")

    Metric.name = "requests_total"

    mock_write_api = MagicMock()
    mock_client = MagicMock()
    mock_client.write_api.return_value = mock_write_api

    with patch("common.metrics.metric.InfluxDBClient", return_value=mock_client):
        Metric.update(42, method="GET", status="200")

    # client initialized
    mock_client.write_api.assert_called_once()

    # write called
    mock_write_api.write.assert_called_once()

    _, kwargs = mock_write_api.write.call_args
    point = kwargs["record"]

    assert isinstance(point, Point)
