"""What the CLI asks of the Dispatcher actor: queue state, kill, reconcile.

The three are methods on the actor, called on its handle, and
``ndif.cli.lib.dispatcher`` is the CLI's side of them. Pure unit tests over
fakes: no Redis, no Ray cluster.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("ray", reason="the dispatcher imports the Ray provider")

from ndif.cli.lib import dispatcher as lib
from ndif.common.schema import Status
from ndif.services.ray.queue.dispatcher import Dispatcher


class Request:
    def __init__(self):
        self.responses = []

    async def arespond(self, status, description):
        self.responses.append((status, description))


class Replica:
    def __init__(self):
        self.cancels = []

    async def cancel(self, message, *, interrupt=False):
        self.cancels.append((message, interrupt))


class Processor:
    def __init__(self, queued=None, executing=None):
        self.queued = queued or {}
        self.executing = executing or {}
        self.reconciles = 0

    def snapshot(self):
        return {"queue_size": len(self.queued)}

    def pop_queued(self, request_id):
        return self.queued.pop(request_id, None)

    def executing_replica(self, request_id):
        return self.executing.get(request_id)

    async def reconcile(self):
        self.reconciles += 1


def dispatcher_with(**processors) -> Dispatcher:
    instance = Dispatcher()
    instance.processors = processors
    return instance


def test_queue_state_is_every_processors_snapshot():
    instance = dispatcher_with(a=Processor(queued={"r1": Request()}), b=Processor())

    assert asyncio.run(instance.queue_state()) == {
        "processors": {"a": {"queue_size": 1}, "b": {"queue_size": 0}}
    }


def test_kill_removes_a_queued_request_and_tells_its_user():
    request = Request()
    processor = Processor(queued={"r1": request})

    reply = asyncio.run(dispatcher_with(a=processor).kill("r1"))

    assert reply["status"] == "removed_from_queue"
    assert processor.queued == {}
    assert request.responses == [(Status.ERROR, "Request cancelled by operator.")]


def test_kill_interrupts_an_executing_request():
    replica = Replica()

    reply = asyncio.run(dispatcher_with(a=Processor(executing={"r1": replica})).kill("r1"))

    assert reply["status"] == "cancelled_execution"
    assert replica.cancels == [("Request cancelled by operator.", True)]


def test_kill_of_an_unknown_request_says_so():
    assert asyncio.run(dispatcher_with(a=Processor()).kill("r1"))["status"] == "not_found"


def test_reconcile_reaches_only_the_named_models_processor():
    a, b = Processor(), Processor()
    instance = dispatcher_with(a=a, b=b)

    asyncio.run(instance.reconcile("a"))
    asyncio.run(instance.reconcile("never-requested"))

    assert (a.reconciles, b.reconciles) == (1, 0)


# --- the CLI's side -----------------------------------------------------------


class Method:
    def __init__(self, calls, name):
        self.calls, self.name = calls, name

    def remote(self, *args):
        self.calls.append((self.name, *args))
        return {"answer": self.name}


class Handle:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        return Method(self.calls, name)


@pytest.fixture
def handle(monkeypatch):
    """A stand-in Dispatcher handle, and the addresses the CLI connected to."""
    import ray

    handle = Handle()
    handle.connected_to = []
    monkeypatch.setattr(lib, "ensure_ray_connected", handle.connected_to.append)
    monkeypatch.setattr(ray, "get", lambda value: value)
    monkeypatch.setattr(
        "ndif.common.providers.ray.get_dispatcher_actor_handle", lambda: handle
    )
    return handle


def test_queue_state_and_kill_connect_and_return_the_dispatchers_answer(handle):
    assert lib.queue_state(ray_address="ray://head:10001") == {"answer": "queue_state"}
    assert lib.kill("r1", ray_address="ray://head:10001") == {"answer": "kill"}

    assert handle.connected_to == ["ray://head:10001"] * 2
    assert handle.calls == [("queue_state",), ("kill", "r1")]


def test_reconcile_names_each_model_once(handle):
    lib.reconcile(["b", "a", "b", None])

    assert handle.calls == [("reconcile", "a"), ("reconcile", "b")]


def test_scale_reconciles_the_model_it_grew(handle, monkeypatch):
    from ndif.cli.lib import scale as scale_lib
    from ndif.common.schema.controller import DeployResponse, ModelDeployResult

    class Scale:
        @staticmethod
        def remote(model_key, n, config):
            return DeployResponse(results={model_key: ModelDeployResult(replicas=["r1"])})

    controller = type("Controller", (), {"scale": Scale})()
    monkeypatch.setattr(scale_lib, "ensure_ray_connected", lambda address: None)
    monkeypatch.setattr(
        "ndif.common.providers.ray.get_controller_actor_handle", lambda: controller
    )

    result = scale_lib.scale(model_key="a", ray_address="ray://head:10001")

    assert result["replicas"] == ["r1"]
    assert handle.calls == [("reconcile", "a")]


def test_reconcile_swallows_a_dispatcher_it_cannot_reach(monkeypatch):
    def missing():
        raise ValueError("Failed to look up actor with name 'Dispatcher'")

    monkeypatch.setattr("ndif.common.providers.ray.get_dispatcher_actor_handle", missing)

    lib.reconcile(["a"])
