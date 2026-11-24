"""Telemetry written under a replica says which replica.

Without the id, a request's points and events name the model and not the copy
of it that ran the request, so nothing shows how many replicas were serving or
whether one of them is the one failing. The controller hands a model actor its
id, the actor puts it on what it writes, and the dispatcher stamps it on the
request so the request's status telemetry carries it too.

Pure unit tests over fakes: no Redis, no Ray cluster, no model.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("ray", reason="the queue and the model actor import Ray")

from ndif.common import metrics
from ndif.common.schema import Status
from ndif.common.schema.request import BackendRequestModel

REPLICA = "abc12"


@pytest.fixture
def written(monkeypatch):
    """Every point handed to Influx, as (measurement, tags, fields)."""
    points = []
    monkeypatch.setattr(metrics.InfluxProvider, "write", lambda *point: points.append(point))
    return points


def test_status_telemetry_carries_the_replica_the_request_was_sent_to(written, caplog):
    request = BackendRequestModel(model_key="m")

    with caplog.at_level(logging.INFO, logger="ndif.request"):
        request._advance_status(Status.RECEIVED)
        request._advance_status(Status.QUEUED)
        request.replica_id = REPLICA  # Replica.dispatch
        request._advance_status(Status.DISPATCHED)
        request._advance_status(Status.RUNNING)

    hops = {tags["status"]: fields.get("replica_id") for _, tags, fields in written}
    # The wait in the queue is recorded as the replica takes the request.
    assert hops == {"RECEIVED": None, "QUEUED": REPLICA, "DISPATCHED": REPLICA}
    stages = {record.stage: getattr(record, "replica_id", None) for record in caplog.records}
    assert stages == {"received": None, "queued": None, "dispatched": REPLICA, "running": REPLICA}


def test_a_request_pickled_before_the_field_existed_still_advances(written):
    """The API and the cluster are not upgraded at the same instant."""
    request = BackendRequestModel(model_key="m")
    del request.__dict__["replica_id"]  # what unpickling an older request leaves

    request._advance_status(Status.RECEIVED)
    request._advance_status(Status.QUEUED)

    assert [tags["status"] for _, tags, _ in written] == ["RECEIVED"]


def test_a_replica_stamps_the_request_before_it_dispatches_it(monkeypatch):
    from ndif.services.ray.queue import replica as replica_module

    seen = []

    class Request:
        id, api_key, email, replica_id = "r", None, None, None

        async def arespond(self, status, description):
            seen.append((status, self.replica_id))

    async def run(request):
        seen.append(("run", request.replica_id))

    actor = SimpleNamespace(run=SimpleNamespace(remote=run))
    monkeypatch.setattr(replica_module, "model_handle", lambda model_key, replica_id: actor)
    pool = SimpleNamespace(queue=asyncio.Queue(), replicas={})
    replica = replica_module.Replica("m", REPLICA, pool)

    asyncio.run(replica.dispatch(Request()))

    assert seen == [(Status.DISPATCHED, REPLICA), ("run", REPLICA)]


def test_the_controller_hands_the_actor_its_replica_id():
    from ndif.services.ray.deployments.controller.cluster.deployment import (
        Deployment,
        DeploymentLevel,
    )
    from ndif.services.ray.deployments.modeling.base import BaseModelDeploymentArgs

    created = []

    class Actor:
        @classmethod
        def options(cls, **options):
            return SimpleNamespace(remote=lambda **arguments: created.append(arguments))

    deployment = Deployment(
        "m", REPLICA, DeploymentLevel.HOT, gpus={0: 1}, size_bytes=1, actor_class=Actor
    )
    deployment.create("node", BaseModelDeploymentArgs(model_key="m"))

    assert created[0]["replica_id"] == REPLICA


def test_the_actor_puts_its_replica_id_on_what_it_reports(written, caplog):
    from ndif.services.ray.deployments.modeling.base import BaseModelDeployment

    actor = object.__new__(BaseModelDeployment)
    actor.model_key, actor.replica_id, actor.execution_timeout = "m", REPLICA, 1.0
    request = SimpleNamespace(id="r", api_key=None, email=None, compress=False)

    with caplog.at_level(logging.WARNING, logger="ndif.modeling"):
        for status in ("cancelled", "preempted", "timeout", "error"):
            actor.report(request, status, 1.0)
    actor.prepare_result(request, b"result")

    assert [record.replica_id for record in caplog.records] == [REPLICA] * 4
    assert [measurement for measurement, _, _ in written] == ["execution_time"] * 4 + ["response_size"]
    assert all(fields["replica_id"] == REPLICA for _, _, fields in written)
