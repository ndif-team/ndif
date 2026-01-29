"""Configuration module for the queue system.

This module centralizes all environment variable configuration for the
Dispatcher and Processor components. It provides type-safe access with
defaults and validation.

The configuration is loaded automatically when the module is imported.
Values can be accessed as class attributes on QueueConfig.

Example:
    >>> from .config import QueueConfig
    >>> redis_url = QueueConfig.broker_url
    >>> reply_freq = QueueConfig.processor_reply_freq_s
"""

import os
from typing import Optional


class QueueConfig:
    """Centralized configuration for the queue system.

    All configuration is loaded from environment variables with sensible
    defaults. Values are validated at load time where applicable.

    Attributes:
        broker_url: Redis connection URL for queue operations.
            Environment variable: NDIF_BROKER_URL
            Default: redis://localhost:6379
        status_cache_freq_s: How long to cache cluster status in Redis (seconds).
            Environment variable: COORDINATOR_STATUS_CACHE_FREQ_S
            Default: 120
        processor_reply_freq_s: Interval between status updates to queued users (seconds).
            Environment variable: COORDINATOR_PROCESSOR_REPLY_FREQ_S
            Default: 3

    Example:
        >>> from .config import QueueConfig
        >>> print(QueueConfig.broker_url)
        'redis://localhost:6379'
    """

    broker_url: Optional[str]
    status_cache_freq_s: int
    processor_reply_freq_s: int
    processor_replica_count: int

    @classmethod
    def from_env(cls) -> None:
        """Load configuration from environment variables.

        Reads all queue-related environment variables and sets them as
        class attributes. Called automatically on module import.

        Raises:
            ValueError: If a required configuration value is missing or invalid.
        """
        cls.broker_url = os.environ.get("NDIF_BROKER_URL", "redis://localhost:6379")

        status_cache_str = os.environ.get("COORDINATOR_STATUS_CACHE_FREQ_S", "120")
        cls.status_cache_freq_s = cls._parse_positive_int(
            status_cache_str, "COORDINATOR_STATUS_CACHE_FREQ_S"
        )

        reply_freq_str = os.environ.get("COORDINATOR_PROCESSOR_REPLY_FREQ_S", "3")
        cls.processor_reply_freq_s = cls._parse_positive_int(
            reply_freq_str, "COORDINATOR_PROCESSOR_REPLY_FREQ_S"
        )

        replica_count_str = os.environ.get("NDIF_MODEL_REPLICAS_DEFAULT", "1")
        cls.processor_replica_count = cls._parse_positive_int(
            replica_count_str, "NDIF_MODEL_REPLICAS_DEFAULT"
        )

    @classmethod
    def to_env(cls) -> dict[str, object]:
        """Export configuration as a dictionary suitable for environment variables.

        Returns:
            Dictionary mapping environment variable names to their current values.
        """
        return {
            "NDIF_BROKER_URL": cls.broker_url,
            "COORDINATOR_STATUS_CACHE_FREQ_S": cls.status_cache_freq_s,
            "COORDINATOR_PROCESSOR_REPLY_FREQ_S": cls.processor_reply_freq_s,
            "NDIF_MODEL_REPLICAS_DEFAULT": cls.processor_replica_count,
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
QueueConfig.from_env()
