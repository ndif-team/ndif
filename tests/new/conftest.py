"""Shared pytest fixtures for the tests/report/ suite.

Module-level helpers (status polling, deploy/evict via lib, response
capture, GPU gating) live in ``_helpers.py`` so test modules can import
them normally — conftest is for fixtures, not for importable utilities.

The suite assumes:

- A live NDIF stack at ``http://localhost:5001`` (override with
  ``--ndif-host``).
- ``NDIF_DEV_MODE=true`` on the API (no API key required, hotswapping
  implicitly granted).
- For the autoscaling tests specifically, the stack must have been brought
  up with short autoscaling timers:

      NDIF_AUTOSCALING_INTERVAL_S=1
      NDIF_AUTOSCALING_WAIT_THRESHOLD_S=5
      NDIF_AUTOSCALING_BACKOFF_S=10

  These are already set in ``docker/docker-compose.yml`` for the test
  stack — production stacks use the QueueConfig defaults (5/30/120).
"""

from __future__ import annotations

import pytest
from nnsight import CONFIG, LanguageModel

from ._helpers import (
    GPT2_REPO,
    LLAMA_8B_REPO,
    LLAMA_70B_REPO,
    QWEN_05B_REPO,
    QWEN_15B_REPO,
    QWEN_7B_REPO,
)


# ---------------------------------------------------------------------------
# Host + nnsight config
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def host(ndif_host) -> str:
    """Shorthand for the parent conftest's ndif_host fixture."""
    return ndif_host


@pytest.fixture(scope="session", autouse=True)
def _configure_nnsight(ndif_host):
    """Point nnsight at the test stack with conservative defaults.

    Sets a dummy API key — the test stack runs with ``NDIF_DEV_MODE=true``
    so the key isn't validated, but nnsight's httpx client still serializes
    it as a header and chokes on ``None`` (the polling path in
    ``RemoteBackend.get_response`` hits the bug first).
    """
    CONFIG.API.HOST = ndif_host
    CONFIG.API.APIKEY = "test-dummy-key"
    CONFIG.APP.REMOTE_LOGGING = False


@pytest.fixture(scope="session", autouse=True)
def _ray_connected(_configure_nnsight):
    """Connect to Ray once for the whole session.

    ``cli.lib.{deploy,evict}`` call ``ensure_ray_connected`` which is a
    no-op when the client is already initialized. Establishing the
    connection at session start (instead of letting each lib call do it)
    sidesteps the Ray proxier's flaky behavior when a fresh client server
    has to be spawned per process — each pytest test runs in one process,
    so we connect once and reuse.
    """
    from ndif.common.providers.ray import RayProvider

    if not RayProvider.connected():
        RayProvider.connect()
    yield
    # Don't disconnect on teardown — leaving Ray alive lets pytest -k filter
    # runs reuse a single warm client across pytest sessions launched back
    # to back from the same shell.


# ---------------------------------------------------------------------------
# Model factories — module-scoped so each test file's LanguageModel
# construction (meta device, no GPU bytes) is shared.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gpt2() -> LanguageModel:
    return LanguageModel(GPT2_REPO)


@pytest.fixture(scope="module")
def qwen_05b() -> LanguageModel:
    return LanguageModel(QWEN_05B_REPO)


@pytest.fixture(scope="module")
def qwen_15b() -> LanguageModel:
    return LanguageModel(QWEN_15B_REPO)


@pytest.fixture(scope="module")
def qwen_7b() -> LanguageModel:
    return LanguageModel(QWEN_7B_REPO)


@pytest.fixture(scope="module")
def llama_8b() -> LanguageModel:
    return LanguageModel(LLAMA_8B_REPO)


@pytest.fixture(scope="module")
def llama_70b() -> LanguageModel:
    return LanguageModel(LLAMA_70B_REPO)
