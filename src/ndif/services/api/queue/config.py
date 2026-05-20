"""Configuration module for the queue system.

This module centralizes all environment variable configuration for the
Dispatcher and Processor components. It provides type-safe access with
defaults and validation.

The configuration is loaded automatically when the module is imported.
Values can be accessed as class attributes on QueueConfig.

Example:
    >>> from .config import QueueConfig
    >>> redis_url = QueueConfig.broker_url
    >>> interval = QueueConfig.autoscaling_interval_s
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
            Environment variable: NDIF_STATUS_CACHE_FREQ_S
            Default: 120
        autoscaling_interval_s: How often each Processor checks the head of
            its queue to decide whether to scale up.
            Environment variable: NDIF_AUTOSCALING_INTERVAL_S
            Default: 5
        autoscaling_wait_threshold_s: A Processor scales up when its oldest
            queued request has been waiting longer than this many seconds.
            Environment variable: NDIF_AUTOSCALING_WAIT_THRESHOLD_S
            Default: 30
        autoscaling_backoff_s: After scaling up, the Processor sleeps for
            this many seconds before re-checking — gives the new replica
            time to come up and absorb queue pressure before another
            scale-up fires.
            Environment variable: NDIF_AUTOSCALING_BACKOFF_S
            Default: 120

    Example:
        >>> from .config import QueueConfig
        >>> print(QueueConfig.broker_url)
        'redis://localhost:6379'
    """

    broker_url: Optional[str]
    status_cache_freq_s: int
    autoscaling_interval_s: int
    autoscaling_wait_threshold_s: int
    autoscaling_backoff_s: int

    @classmethod
    def from_env(cls) -> None:
        """Load configuration from environment variables.

        Reads all queue-related environment variables and sets them as
        class attributes. Called automatically on module import.

        Raises:
            ValueError: If a required configuration value is missing or invalid.
        """
        cls.broker_url = os.environ.get("NDIF_BROKER_URL", "redis://localhost:6379")

        status_cache_str = os.environ.get("NDIF_STATUS_CACHE_FREQ_S", "120")
        cls.status_cache_freq_s = cls._parse_positive_int(
            status_cache_str, "NDIF_STATUS_CACHE_FREQ_S"
        )

        cls.autoscaling_interval_s = cls._parse_positive_int(
            os.environ.get("NDIF_AUTOSCALING_INTERVAL_S", "5"),
            "NDIF_AUTOSCALING_INTERVAL_S",
        )
        cls.autoscaling_wait_threshold_s = cls._parse_positive_int(
            os.environ.get("NDIF_AUTOSCALING_WAIT_THRESHOLD_S", "30"),
            "NDIF_AUTOSCALING_WAIT_THRESHOLD_S",
        )
        cls.autoscaling_backoff_s = cls._parse_positive_int(
            os.environ.get("NDIF_AUTOSCALING_BACKOFF_S", "120"),
            "NDIF_AUTOSCALING_BACKOFF_S",
        )

    @classmethod
    def to_env(cls) -> dict[str, object]:
        """Export configuration as a dictionary suitable for environment variables.

        Returns:
            Dictionary mapping environment variable names to their current values.
        """
        return {
            "NDIF_BROKER_URL": cls.broker_url,
            "NDIF_STATUS_CACHE_FREQ_S": cls.status_cache_freq_s,
            "NDIF_AUTOSCALING_INTERVAL_S": cls.autoscaling_interval_s,
            "NDIF_AUTOSCALING_WAIT_THRESHOLD_S": cls.autoscaling_wait_threshold_s,
            "NDIF_AUTOSCALING_BACKOFF_S": cls.autoscaling_backoff_s,
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
