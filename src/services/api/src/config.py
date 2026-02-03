"""Configuration module for the API service.

This module centralizes all environment variable configuration for the
FastAPI application, including Redis connection, SocketIO settings, and
request handling parameters.

The configuration is loaded automatically when the module is imported.
Values can be accessed as class attributes on AppConfig.

Example:
    >>> from .config import AppConfig
    >>> redis_url = AppConfig.broker_url
    >>> buffer_size = AppConfig.socketio_max_http_buffer_size
"""

import os
import sys
from typing import Optional

from nnsight import __version__ as nnsight_version


class AppConfig:
    """Centralized configuration for the API service.

    All configuration is loaded from environment variables with sensible
    defaults. Values are validated at load time where applicable.

    Attributes:
        broker_url: Redis connection URL for queue and pub/sub operations.
            Environment variable: NDIF_BROKER_URL
            Default: redis://localhost:6379
        socketio_max_http_buffer_size: Maximum size in bytes for SocketIO
            HTTP buffer. Limits the size of incoming messages.
            Environment variable: SOCKETIO_MAX_HTTP_BUFFER_SIZE
            Default: 100_000_000 (100 MB)
        socketio_ping_timeout: Timeout in seconds for SocketIO ping/pong.
            Environment variable: SOCKETIO_PING_TIMEOUT
            Default: 60
        status_request_timeout_s: Maximum time to wait for cluster status
            response before timing out.
            Environment variable: STATUS_REQUEST_TIMEOUT_S
            Default: 60

    Example:
        >>> from .config import AppConfig
        >>> print(AppConfig.broker_url)
        'redis://localhost:6379'
    """

    broker_url: Optional[str]
    socketio_max_http_buffer_size: int
    socketio_ping_timeout: int
    status_request_timeout_s: int
    min_nnsight_version: str
    min_python_version: str
    dev_mode: bool

    @classmethod
    def from_env(cls) -> None:
        """Load configuration from environment variables.

        Reads all API-related environment variables and sets them as
        class attributes. Called automatically on module import.

        Raises:
            ValueError: If a configuration value is invalid.
        """
        cls.broker_url = os.environ.get("NDIF_BROKER_URL", "redis://localhost:6379")

        buffer_size_str = os.environ.get("SOCKETIO_MAX_HTTP_BUFFER_SIZE", "100000000")
        cls.socketio_max_http_buffer_size = cls._parse_positive_int(
            buffer_size_str, "SOCKETIO_MAX_HTTP_BUFFER_SIZE"
        )

        ping_timeout_str = os.environ.get("SOCKETIO_PING_TIMEOUT", "60")
        cls.socketio_ping_timeout = cls._parse_positive_int(
            ping_timeout_str, "SOCKETIO_PING_TIMEOUT"
        )

        status_timeout_str = os.environ.get("STATUS_REQUEST_TIMEOUT_S", "60")
        cls.status_request_timeout_s = cls._parse_positive_int(
            status_timeout_str, "STATUS_REQUEST_TIMEOUT_S"
        )

        cls.min_nnsight_version = os.environ.get("MIN_NNSIGHT_VERSION", nnsight_version)
        cls.min_python_version = os.environ.get(
            "MIN_PYTHON_VERSION", ".".join(sys.version.split(".")[0:2])
        )
        cls.dev_mode = os.environ.get("NDIF_DEV_MODE", "false").lower() == "true"

    @classmethod
    def to_env(cls) -> dict[str, object]:
        """Export configuration as a dictionary suitable for environment variables.

        Returns:
            Dictionary mapping environment variable names to their current values.
        """
        return {
            "NDIF_BROKER_URL": cls.broker_url,
            "SOCKETIO_MAX_HTTP_BUFFER_SIZE": cls.socketio_max_http_buffer_size,
            "SOCKETIO_PING_TIMEOUT": cls.socketio_ping_timeout,
            "STATUS_REQUEST_TIMEOUT_S": cls.status_request_timeout_s,
            "MIN_NNSIGHT_VERSION": cls.min_nnsight_version,
            "MIN_PYTHON_VERSION": cls.min_python_version,
            "NDIF_DEV_MODE": cls.dev_mode,
        }

    @classmethod
    def _parse_positive_int(cls, value: str, name: str) -> int:
        """Parse a string as a positive integer.

        Args:
            value: The string value to parse.
            name: The environment variable name (for error messages).

        Returns:
            The parsed integer value.

        Raises:
            ValueError: If the value cannot be parsed or is not positive.
        """
        try:
            result = int(value)
        except ValueError:
            raise ValueError(
                f"Invalid value for {name}: '{value}' is not a valid integer"
            )

        if result <= 0:
            raise ValueError(
                f"Invalid value for {name}: {result} must be a positive integer"
            )

        return result


# Load configuration on module import
AppConfig.from_env()
