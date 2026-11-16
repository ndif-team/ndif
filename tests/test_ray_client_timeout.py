"""Ray Client calls have to give up when the Ray connection is dead.

Over Ray Client, an actor lookup or a call's id in flight when the head dies
never arrives. The dispatcher makes these calls on its event loop, so before the
bound one froze the dispatcher until the API restarted. ``RayProvider.connected``
has to report False in the same situation, and ``reset`` has to return, or the
dispatcher never reconnects.

No Ray: the client calls are fakes that block until the test releases them.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("ray", reason="the provider imports Ray")

from ndif.common.providers import ray as ray_provider


@pytest.fixture
def release(monkeypatch):
    event = threading.Event()
    monkeypatch.setattr(ray_provider, "RAY_CLIENT_TIMEOUT_S", 0.2)
    yield event
    event.set()


def test_hung_lookup_times_out(release, monkeypatch):
    monkeypatch.setattr(ray_provider.ray, "get_actor", lambda *a, **k: release.wait())

    start = time.monotonic()
    with pytest.raises(TimeoutError, match="'Controller'"):
        ray_provider.get_named_actor("Controller")
    assert time.monotonic() - start < 2


def test_hung_lookup_means_not_connected(release, monkeypatch):
    monkeypatch.setattr(ray_provider.ray, "get_actor", lambda *a, **k: release.wait())
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


def test_hung_call_times_out(release, monkeypatch):
    ref = SimpleNamespace(binary=release.wait)
    monkeypatch.setattr(
        ray_provider, "client_ray", SimpleNamespace(call_remote=lambda *a, **k: ref)
    )
    monkeypatch.setattr(ray_provider, "return_refs", lambda refs: refs)

    method = ray_provider.NDIFClientRemoteMethod(None, "status")
    with pytest.raises(TimeoutError, match=r"status\.remote\(\)"):
        method.remote()


def test_call_returns_ref_with_its_id(monkeypatch):
    ref = SimpleNamespace(binary=lambda: b"id")
    monkeypatch.setattr(
        ray_provider, "client_ray", SimpleNamespace(call_remote=lambda *a, **k: ref)
    )
    monkeypatch.setattr(ray_provider, "return_refs", lambda refs: refs)

    assert ray_provider.NDIFClientRemoteMethod(None, "status").remote() is ref


def test_reset_closes_the_channel_before_shutdown(monkeypatch):
    calls = []
    channel = SimpleNamespace(close=lambda: calls.append("close"))
    worker = SimpleNamespace(channel=channel)
    monkeypatch.setattr(
        ray_provider,
        "client_ray",
        SimpleNamespace(get_context=lambda: SimpleNamespace(client_worker=worker)),
    )
    monkeypatch.setattr(ray_provider.ray, "shutdown", lambda: calls.append("shutdown"))

    ray_provider.RayProvider.reset()
    assert calls == ["close", "shutdown"]
