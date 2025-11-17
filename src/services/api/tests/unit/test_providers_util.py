from unittest.mock import MagicMock, patch

from src.providers.util import verify_connection


def test_verify_connection_success():
    with patch("socket.create_connection") as mock_conn:
        mock_conn.return_value.__enter__.return_value = MagicMock()
        assert verify_connection("127.0.0.1", 8000) is True
        mock_conn.assert_called_once_with(("127.0.0.1", 8000), timeout=2)
