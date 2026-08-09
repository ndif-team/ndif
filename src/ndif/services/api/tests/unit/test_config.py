"""Unit tests for API configuration parsing helpers."""

import pytest

from ndif.services.api.config import AppConfig


def test_parse_positive_int() -> None:
    assert AppConfig._parse_positive_int("12", "EXAMPLE_LIMIT") == 12


def test_parse_positive_int_rejects_zero() -> None:
    with pytest.raises(ValueError, match="EXAMPLE_LIMIT: 0 must be a positive integer"):
        AppConfig._parse_positive_int("0", "EXAMPLE_LIMIT")
