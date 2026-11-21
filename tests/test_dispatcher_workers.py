"""The dispatcher's loops have to outlive a Redis outage, and say they are alive.

``status_worker``, ``env_worker`` and ``events_worker`` are started once and
nothing restarts them, so an error that escapes one ends it for the life of the
dispatcher: ``/status`` and ``/env`` then stay broken after Redis comes back.
While Redis is down every read also fails at once, so a loop with no pause
between retries spins, measured at thousands of reads a second.

The dispatcher runs inside the Ray cluster and Redis can be across a WAN from
it, so no read may wait forever, and the API has only the ``ray:connected`` key
to tell it that the dispatcher is alive.

Pure unit tests over fakes: no Redis, no Ray, no server.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("ray", reason="the dispatcher imports the Ray provider")

from redis.exceptions import ConnectionError as RedisConnectionError

from ndif.services.ray.queue import dispatcher as dispatcher_module


class RedisDown:
    """Every command fails. The third read ends the worker under test."""

    def __init__(self):
        self.reads = 0
        self.blocks = []

    async def xrevrange(self, stream, count=None):
        return []

    async def xread(self, streams, block=None, **kwargs):
        self.reads += 1
        self.blocks.append(block)
        self.ids = list(streams.values())
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
    instance = dispatcher_module.Dispatcher()
    return instance, sleeps


async def idle():
    pass


@pytest.mark.parametrize("worker", ["status_worker", "env_worker", "events_worker"])
def test_side_workers_survive_redis_going_away(dispatcher, monkeypatch, worker):
    instance, sleeps = dispatcher
    redis = RedisDown()
    monkeypatch.setattr(dispatcher_module.RedisProvider, "async_client", redis, raising=False)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(getattr(instance, worker)())

    assert redis.reads == 3, "the worker stopped retrying"
    assert sleeps == [1, 1], "retried with no pause"
    assert all(block and block > 0 for block in redis.blocks), "a read with no limit"
    # A bounded read is issued again and again, and from "$" each would miss
    # what was added since the last one returned.
    assert redis.ids == ["0-0"]


def test_a_stream_is_read_on_from_its_newest_entry():
    class Redis:
        async def xrevrange(self, stream, count=None):
            return [("1700000000000-3", {"event_type": "kill_request"})]

    assert asyncio.run(dispatcher_module.stream_tail(Redis(), "s")) == "1700000000000-3"


def test_main_loop_pauses_between_failures(dispatcher):
    instance, sleeps = dispatcher
    calls = []

    async def get():
        calls.append(1)
        if len(calls) == 3:
            raise asyncio.CancelledError
        raise RedisConnectionError("Redis went away")

    instance.get = get
    instance.startup = instance.heartbeat_worker = idle
    instance.status_worker = instance.env_worker = instance.events_worker = idle

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(instance.dispatch_worker())

    assert sleeps == [1, 1], "retried with no pause"


def test_heartbeat_worker_renews_the_connected_key_before_it_expires(dispatcher, monkeypatch):
    """The key is all the API has, and it must lapse when the dispatcher is gone."""
    instance, sleeps = dispatcher
    sets = []

    class Redis:
        async def set(self, key, value, ex=None):
            sets.append((key, ex))
            if len(sets) == 2:
                raise RedisConnectionError("Redis went away")
            if len(sets) == 4:
                raise asyncio.CancelledError

    monkeypatch.setattr(dispatcher_module.RedisProvider, "async_client", Redis(), raising=False)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(instance.heartbeat_worker())

    ttl = dispatcher_module.CONNECTED_TTL_S
    assert sets == [("ray:connected", ttl)] * 4
    # Renewed well inside the expiry, and retried soon after a failure.
    assert sleeps == [ttl / 3, 1, ttl / 3]


def test_startup_waits_for_redis_and_then_for_the_controller(dispatcher, monkeypatch):
    instance, sleeps = dispatcher
    deletes = []

    class Redis:
        async def delete(self, *keys):
            deletes.append(keys)
            if len(deletes) == 1:
                raise RedisConnectionError("Redis is not up yet")

    lookups = []

    def controller_handle():
        lookups.append(1)
        if len(lookups) < 3:
            raise ValueError("Failed to look up actor with name 'Controller'")

    monkeypatch.setattr(dispatcher_module.RedisProvider, "async_client", Redis(), raising=False)
    monkeypatch.setattr(dispatcher_module, "controller_handle", controller_handle)

    asyncio.run(instance.startup())

    assert len(deletes) == 2 and len(lookups) == 3
    assert sleeps == [1, 1, 1]
