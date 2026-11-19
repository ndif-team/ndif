"""Ray Client calls have to give up when the Ray connection is dead.

Over Ray Client, an actor lookup or a call's id in flight when the head dies
never arrives. The dispatcher makes these calls on its event loop, so before the
bound one froze the dispatcher until the API restarted. ``RayProvider.connected``
has to report False in the same situation, and ``reset`` has to return, or the
dispatcher never reconnects.

The queue makes its calls from one event loop, which has to keep running while
a call waits out the limit.

A wait that was cancelled must not keep Ray from telling the other waiters that
the connection closed, or they wait forever.

No Ray cluster: the client calls are fakes that block until the test releases
them.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import Future
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


def test_failed_first_step_means_not_connected(monkeypatch):
    """On a new connection ``ray.is_initialized`` asks the head, and raises if it has gone."""

    def gone():
        raise ConnectionError("the head went away")

    monkeypatch.setattr(ray_provider.ray, "is_initialized", gone)

    assert ray_provider.RayProvider.connected() is False
    assert asyncio.run(ray_provider.RayProvider.aconnected()) is False


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
        ray_provider, "client_ray", SimpleNamespace(call_remote=lambda *a, **k: [ref])
    )
    monkeypatch.setattr(ray_provider, "NDIFObjectRef", lambda ref: ref)

    method = ray_provider.NDIFClientRemoteMethod(None, "status")
    with pytest.raises(TimeoutError, match=r"status\.remote\(\)"):
        method.remote()


def test_call_returns_ref_with_its_id(monkeypatch):
    ref = SimpleNamespace(binary=lambda: b"id")
    monkeypatch.setattr(
        ray_provider, "client_ray", SimpleNamespace(call_remote=lambda *a, **k: [ref])
    )
    monkeypatch.setattr(ray_provider, "NDIFObjectRef", lambda ref: ref)

    assert ray_provider.NDIFClientRemoteMethod(None, "status").remote() is ref


def test_hung_call_on_an_event_loop_times_out_while_the_loop_runs(release, monkeypatch):
    # Returns in the end, so that a missing limit fails this test and not hangs it.
    monkeypatch.setattr(ray_provider.ray, "get_actor", lambda *a, **k: release.wait(10))

    async def main():
        turns = 0

        async def count():
            nonlocal turns
            while True:
                await asyncio.sleep(0.01)
                turns += 1

        counting = asyncio.create_task(count())
        with pytest.raises(TimeoutError, match=r"Controller\.status\.remote\(\)"):
            await ray_provider.controller_handle().status.remote()
        counting.cancel()
        return turns

    assert asyncio.run(main()) > 0, "the event loop did not run during the call"


@pytest.mark.parametrize("ray_client", [True, False])
def test_call_on_an_event_loop_returns_its_result(monkeypatch, ray_client):
    result = Future()
    result.set_result("status")

    def send(*args):
        assert args == ("argument",)
        return SimpleNamespace(future=lambda: result)

    # Over Ray Client the method is NDIF's, whose ``remote`` would block again.
    method = (
        SimpleNamespace(send=send, remote=lambda *a: pytest.fail("blocking remote()"))
        if ray_client
        else SimpleNamespace(remote=send)
    )
    monkeypatch.setattr(
        ray_provider.ray, "get_actor", lambda *a, **k: SimpleNamespace(status=method)
    )

    call = ray_provider.controller_handle().status.remote("argument")
    assert asyncio.run(call) == "status"


def test_lookup_error_on_an_event_loop_is_raised_by_the_call(monkeypatch):
    def missing(name, namespace):
        raise ValueError(f"Failed to look up actor with name '{name}'")

    monkeypatch.setattr(ray_provider.ray, "get_actor", missing)
    handle = ray_provider.model_handle("model", "gone")

    with pytest.raises(ValueError, match="gone:ModelActor:model"):
        asyncio.run(handle.run.remote())


def test_cancelled_wait_does_not_keep_ray_from_telling_the_rest(monkeypatch):
    """Ray's own shutdown loop, over a cancelled waiter and one registered after it."""
    from ray.util.client.dataclient import DataClient

    data_client = DataClient.__new__(DataClient)
    data_client.lock = threading.Lock()
    data_client.cv = threading.Condition(data_client.lock)
    data_client._last_exception = None
    data_client.asyncio_waiting_data = {}

    def register_callback(ref, callback):
        waiting = data_client.asyncio_waiting_data
        waiting[len(waiting)] = callback

    worker = SimpleNamespace(
        register_callback=register_callback, is_connected=lambda: False
    )
    monkeypatch.setattr(
        ray_provider.client_ray,
        "get_context",
        lambda: SimpleNamespace(client_worker=worker),
    )

    cancelled = ray_provider.NDIFObjectRef(Future()).future()
    waiting = ray_provider.NDIFObjectRef(Future()).future()
    cancelled.cancel()

    data_client._shutdown()

    assert isinstance(waiting.exception(timeout=1), ConnectionError)


def test_connection_check_on_an_event_loop_gives_up_at_the_limit(release, monkeypatch):
    monkeypatch.setattr(
        ray_provider.RayProvider, "connected", lambda: release.wait(10)
    )

    assert asyncio.run(ray_provider.RayProvider.aconnected()) is False


@pytest.mark.parametrize("answers", [True, False])
def test_an_error_reconnects_only_if_the_connection_check_fails(monkeypatch, answers):
    """An error from a connection that was since replaced arrives after the
    reconnect, and reads like any connection error."""
    from ndif.services.ray.queue import dispatcher as dispatcher_module

    connects = []
    monkeypatch.setattr(ray_provider.RayProvider, "connected", lambda: answers)
    dispatcher = dispatcher_module.Dispatcher.__new__(dispatcher_module.Dispatcher)
    dispatcher.processors = {}
    dispatcher.error_queue = asyncio.Queue()
    dispatcher.error_queue.put_nowait(
        ("m", ConnectionError("Failed during this or a previous request."))
    )
    dispatcher.connect = lambda: connects.append(1)

    asyncio.run(dispatcher.handle_errors())

    assert bool(connects) is not answers


@pytest.mark.parametrize("answers", [[True], [False, True]])
def test_connect_resets_before_it_checks(monkeypatch, answers):
    """It runs where the connection is absent or was just found dead, and a check
    of a dead connection waits out the limit."""
    from ndif.services.ray.queue import dispatcher as dispatcher_module

    calls = []
    redis = SimpleNamespace(
        connected=lambda: True,
        sync_client=SimpleNamespace(
            delete=lambda *keys: None, set=lambda key, value: calls.append(key)
        ),
    )
    ray = SimpleNamespace(
        reset=lambda: calls.append("reset"),
        connect=lambda: calls.append("connect"),
        connected=lambda: calls.append("check") or answers.pop(0),
    )
    monkeypatch.setattr(dispatcher_module, "RedisProvider", redis)
    monkeypatch.setattr(dispatcher_module, "RayProvider", ray)
    monkeypatch.setattr(dispatcher_module.time, "sleep", lambda seconds: None)
    attempts = len(answers)

    dispatcher_module.Dispatcher.connect(
        dispatcher_module.Dispatcher.__new__(dispatcher_module.Dispatcher)
    )

    assert calls == ["reset", "connect", "check"] * attempts + ["ray:connected"]


def test_purge_cancels_a_start_still_under_way(monkeypatch):
    """A ``start()`` from before a purge fails once the new connection answers,
    and would then purge the requests that came after it."""
    from ndif.services.ray.queue import processor as processor_module

    responses = []

    async def arespond(status, description=""):
        responses.append(getattr(status, "name", status))

    request = SimpleNamespace(
        id="after",
        priority=False,
        enqueued_at=None,
        trust_remote_code=False,
        api_key=None,
        email=None,
        arespond=arespond,
    )

    async def main():
        old_connection = asyncio.Event()
        calls = []

        async def get_deployment(model_key):
            calls.append(model_key)
            if len(calls) == 1:
                await old_connection.wait()
                raise ValueError("no such actor on the new head")
            await asyncio.Event().wait()  # the new start() is still under way

        controller = SimpleNamespace(
            get_deployment=SimpleNamespace(remote=get_deployment)
        )
        monkeypatch.setattr(processor_module, "controller_handle", lambda: controller)

        processor = processor_module.Processor("model", asyncio.Queue())
        processor.ensure_started()
        await asyncio.sleep(0)
        await processor.purge("the connection was lost")

        await processor.enqueue(request)
        old_connection.set()
        await asyncio.sleep(0.05)

        processor.autoscaling_task.cancel()
        processor.start_task.cancel()
        return len(calls)

    starts = asyncio.run(main())
    assert starts == 2
    assert "ERROR" not in responses


def test_reset_closes_the_channel_before_shutdown(monkeypatch):
    calls = []
    # Shutting down by the time the channel closes, or Ray tries to reconnect it.
    worker = SimpleNamespace(_in_shutdown=False)
    worker.channel = SimpleNamespace(
        close=lambda: calls.append("close" if worker._in_shutdown else "close early")
    )
    monkeypatch.setattr(
        ray_provider,
        "client_ray",
        SimpleNamespace(get_context=lambda: SimpleNamespace(client_worker=worker)),
    )
    monkeypatch.setattr(ray_provider.ray, "shutdown", lambda: calls.append("shutdown"))

    ray_provider.RayProvider.reset()
    assert calls == ["close", "shutdown"]
