"""The dispatcher's loops have to outlive a Redis outage.

``status_worker``, ``env_worker`` and ``events_worker`` are started once and
nothing restarts them, so an error that escapes one ends it until the API
restarts: ``/status`` and ``/env`` then stay broken after Redis comes back. While
Redis is down every read also fails at once, so a loop with no pause between
retries spins, measured at thousands of reads a second.

Pure unit tests over fakes: no Redis, no Ray, no server.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("ray", reason="the dispatcher imports the Ray provider")

from redis.exceptions import ConnectionError as RedisConnectionError

from ndif.services.api.queue import dispatcher as dispatcher_module


class RedisDown:
    """Every command fails. The third read ends the worker under test."""

    def __init__(self):
        self.reads = 0

    async def xread(self, *args, **kwargs):
        self.reads += 1
        if self.reads == 3:
            raise asyncio.CancelledError
        raise RedisConnectionError("Redis went away")

    async def delete(self, *args, **kwargs):
        raise RedisConnectionError("Redis went away")

    async def publish(self, *args, **kwargs):
        raise RedisConnectionError("Redis went away")


@pytest.fixture
def dispatcher(monkeypatch):
    """A Dispatcher with no connections, recording the pauses it takes."""
    sleeps = []

    async def record_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(dispatcher_module.asyncio, "sleep", record_sleep)
    instance = dispatcher_module.Dispatcher.__new__(dispatcher_module.Dispatcher)
    instance.error_queue = asyncio.Queue()
    return instance, sleeps


@pytest.mark.parametrize("worker", ["status_worker", "env_worker", "events_worker"])
def test_side_workers_survive_redis_going_away(dispatcher, monkeypatch, worker):
    instance, sleeps = dispatcher
    redis = RedisDown()
    monkeypatch.setattr(dispatcher_module.RedisProvider, "async_client", redis, raising=False)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(getattr(instance, worker)())

    assert redis.reads == 3, "the worker stopped retrying"
    assert sleeps == [1, 1], "retried with no pause"


def test_main_loop_pauses_between_failures(dispatcher):
    instance, sleeps = dispatcher
    calls = []

    async def get():
        calls.append(1)
        if len(calls) == 3:
            raise asyncio.CancelledError
        raise RedisConnectionError("Redis went away")

    async def idle():
        pass

    instance.get = get
    instance.status_worker = instance.env_worker = instance.events_worker = idle

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(instance.dispatch_worker())

    assert sleeps == [1, 1], "retried with no pause"
