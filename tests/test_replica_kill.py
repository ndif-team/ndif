"""An operator kill of an executing request stops the request and keeps the replica.

``ndif kill`` on a running request goes ``Dispatcher.kill`` ->
``Replica.kill`` -> the actor's ``cancel``. The actor ignores
a cancel that reaches it before the block has begun, and it can be slow to stop
one. Neither may cost the pool a healthy replica. A batch of kills on a busy
model once left the dispatcher using 8 of its 10 replicas, and only a manual
reconcile brought the other two back.

Pure unit tests over fakes: no Redis, no Ray cluster.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("ray", reason="the replica imports the Ray provider")

from ndif.common.schema import Status
from ndif.services.ray.queue import replica as replica_module
from ndif.services.ray.queue.replica import Replica

OPERATOR = "Request cancelled by operator."


class Request:
    def __init__(self, id: str):
        self.id, self.api_key, self.email = id, None, None
        self.responses = []

    async def arespond(self, status, description):
        self.responses.append((status, description))


class Actor:
    """A model actor whose block runs until a ``cancel`` it honours.

    ``honours`` is which cancel that is, counting from 1: the earlier ones stand
    for cancels that arrive before the block has begun. ``None`` is an actor
    that never stops it.
    """

    def __init__(self, honours: int | None):
        self.honours = honours
        self.cancels = 0
        self.ran = []
        self.stopped = asyncio.Event()
        self.run = SimpleNamespace(remote=self._run)
        self.cancel = SimpleNamespace(remote=self._cancel)

    async def _run(self, request) -> None:
        self.ran.append(request.id)
        if request.id == "killed":
            await self.stopped.wait()

    async def _cancel(self) -> None:
        self.cancels += 1
        if self.cancels == self.honours:
            self.stopped.set()


class Pool:
    """What a Replica needs of its Processor."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.replicas = {}

    def ensure_started(self) -> None:
        pass

    def mark_idle(self) -> None:
        pass


async def until(condition) -> None:
    async def poll():
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), 5)


def serving(monkeypatch, honours):
    """Inside a running loop: a replica executing ``killed`` with ``next`` queued."""
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: real_sleep(0))
    actor, pool = Actor(honours), Pool()
    monkeypatch.setattr(replica_module, "model_handle", lambda model_key, replica_id: actor)
    replica = pool.replicas["r1"] = Replica("model", "r1", pool)
    killed = Request("killed")
    pool.queue.put_nowait(killed)
    pool.queue.put_nowait(Request("next"))
    replica.start()
    return actor, pool, replica, killed


def test_a_kill_is_repeated_until_the_actor_honours_it(monkeypatch):
    async def scenario():
        actor, pool, replica, killed = serving(monkeypatch, honours=3)
        await until(lambda: actor.ran == ["killed"])

        await replica.kill(OPERATOR)

        assert actor.cancels == 3
        assert killed.responses[-1] == (Status.ERROR, OPERATOR)
        # The replica is still in the pool and takes the next request.
        await until(lambda: actor.ran == ["killed", "next"])
        assert pool.replicas == {"r1": replica} and not replica.dropped

    asyncio.run(scenario())


def test_a_kill_the_actor_never_honours_keeps_the_replica(monkeypatch):
    async def scenario():
        actor, pool, replica, killed = serving(monkeypatch, honours=None)
        await until(lambda: actor.ran == ["killed"])

        await replica.kill(OPERATOR)

        # It stopped asking, and left the worker waiting on the block.
        assert actor.cancels == 50
        assert [status for status, _ in killed.responses].count(Status.ERROR) == 1
        assert pool.replicas == {"r1": replica} and replica.current_request is killed
        # When the block does return, the replica serves on.
        actor.stopped.set()
        await until(lambda: actor.ran == ["killed", "next"])
        assert pool.replicas == {"r1": replica} and not replica.dropped

    asyncio.run(scenario())


def test_a_purge_still_drops_the_replica(monkeypatch):
    async def scenario():
        actor, pool, replica, killed = serving(monkeypatch, honours=None)
        await until(lambda: actor.ran == ["killed"])

        await replica.cancel("purged")
        await until(lambda: replica.dropped)

        assert actor.cancels == 0 and pool.replicas == {}
        assert killed.responses[-2][1] == "purged"

    asyncio.run(scenario())
