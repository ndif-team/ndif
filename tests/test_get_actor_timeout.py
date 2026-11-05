"""A named-actor lookup has to give up when the Ray connection is dead.

Over Ray Client, ``ray.get_actor`` in flight when the head dies never returns.
The dispatcher calls it on its event loop, so before the bound one lookup froze
the dispatcher until the API restarted. ``RayProvider.connected`` has to report
False in the same situation, or the dispatcher never reconnects.

No Ray: ``ray.get_actor`` is a fake that blocks until the test releases it.
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("ray", reason="the provider imports Ray")

from ndif.common.providers import ray as ray_provider


@pytest.fixture
def hung_lookup(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(ray_provider, "GET_ACTOR_TIMEOUT_S", 0.2)
    monkeypatch.setattr(
        ray_provider.ray, "get_actor", lambda *args, **kwargs: release.wait()
    )
    yield
    release.set()


def test_hung_lookup_times_out(hung_lookup):
    start = time.monotonic()
    with pytest.raises(TimeoutError, match="'Controller'"):
        ray_provider.get_named_actor("Controller")
    assert time.monotonic() - start < 2


def test_hung_lookup_means_not_connected(hung_lookup, monkeypatch):
    monkeypatch.setattr(ray_provider.ray, "is_initialized", lambda: True)
    monkeypatch.setattr(ray_provider.RayProvider, "is_listening", lambda: True)

    assert ray_provider.RayProvider.connected() is False


def test_lookup_error_propagates(monkeypatch):
    def missing(name, namespace):
        raise ValueError(f"Failed to look up actor with name '{name}'")

    monkeypatch.setattr(ray_provider.ray, "get_actor", missing)
    with pytest.raises(ValueError, match="Failed to look up actor"):
        ray_provider.get_named_actor("1:ModelActor:gone")


def test_lookup_returns_handle(monkeypatch):
    handle = object()
    monkeypatch.setattr(ray_provider.ray, "get_actor", lambda *a, **k: handle)

    assert ray_provider.get_named_actor("Controller") is handle
