"""Fixtures for cross-NUMA penalty benchmarks."""

import platform
from pathlib import Path

import pytest


@pytest.fixture
def benchmark_model(request) -> str:
    """Model name for model loading benchmarks."""
    return request.config.getoption("--benchmark-model")


@pytest.fixture
def benchmark_iterations(request) -> int:
    """Number of iterations for benchmarks."""
    quick = request.config.getoption("--quick", default=False)
    return 3 if quick else 10
