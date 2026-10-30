"""Regression tests for the two bugs behind the 2026-09-08 prod outage.

A slurm walltime kill removed every GPU worker at once. Neither bug caused the
outage, but together they made it silent for an hour: the controller never
noticed the nodes were gone, so ``/status`` kept advertising a full cluster and
the dashboard's down alert never fired, while the only notification that did go
out — the model-trace ping — said nothing but "Traceback (most recent call
last):" for all 23 models.
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from ndif.services.ray.deployments.controller.cluster import cluster as cluster_mod
from ndif.services.ray.deployments.controller.cluster.cluster import Cluster
from ndif.services.ray.deployments.controller import controller as controller_mod
from ndif.services.ray.deployments.controller.controller import _ControllerActor
from ndif.services.dashboard.jobs.util import summarize_error


@pytest.fixture(autouse=True)
def no_ray(monkeypatch):
    """``update_nodes`` reads the GCS address off the runtime context."""
    monkeypatch.setattr(
        cluster_mod.ray,
        "get_runtime_context",
        lambda: SimpleNamespace(gcs_address="10.9.8.7:7111"),
    )


def _node(ip, gpus=4, state="ALIVE"):
    """A NodeState stand-in. Ray reports DEAD nodes with resources intact, so
    these deliberately carry full ``resources_total`` regardless of state."""
    return SimpleNamespace(
        node_id=f"id-{ip}",
        node_name=ip,
        state=state,
        labels={"ray.io/accelerator-type": "A40"},
        resources_total={
            "GPU": float(gpus),
            "cuda_memory_bytes": 47699722240.0 * gpus,
            "cpu_memory_bytes": 248423067648.0,
        },
    )


def test_dead_node_record_is_purged_without_failing_the_sync(monkeypatch):
    """A node going DEAD stays in ``list_nodes``, with ``"GPU"`` still in its
    ``resources_total``. It must be dropped, and the listing must not raise.

    The stand-in behaves as Ray does: a ``state`` filter counts the nodes it
    removes as truncated output, and the strict default raises on that for as
    long as any DEAD record exists.
    """
    records = [_node("10.0.0.1"), _node("10.0.0.2")]

    def fake_list_nodes(**kwargs):
        if kwargs.get("filters") and any(n.state != "ALIVE" for n in records):
            raise RuntimeError("Failed to retrieve all nodes: data truncation")
        return list(records)

    monkeypatch.setattr(cluster_mod, "list_nodes", fake_list_nodes)

    c = Cluster()
    c.update_nodes()
    assert len(c.nodes) == 2

    records[1].state = "DEAD"
    c.update_nodes()
    assert [n.name for n in c.nodes.values()] == ["10.0.0.1"]


def test_departed_node_is_purged(monkeypatch):
    """A node that stops being reported is dropped from the registry."""
    live = [_node("10.0.0.1"), _node("10.0.0.2")]
    monkeypatch.setattr(cluster_mod, "list_nodes", lambda **kw: list(live))

    c = Cluster()
    c.update_nodes()
    assert len(c.nodes) == 2

    live.pop()  # the walltime kill
    c.update_nodes()
    assert [n.name for n in c.nodes.values()] == ["10.0.0.1"]


def test_check_nodes_survives_a_failed_sync(monkeypatch):
    """A failed sync is retried. Nothing awaits this task, so an error that
    escapes it ends node syncing for the life of the controller; on 2026-10-30
    a network drop on two nodes did that through ``list_nodes``."""
    monkeypatch.setenv("NDIF_CONTROLLER_SYNC_INTERVAL_S", "0")
    calls = []

    def update_nodes():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("Failed to retrieve all 9 nodes from the cluster")
        raise asyncio.CancelledError  # ends the loop

    controller = SimpleNamespace(cluster=SimpleNamespace(update_nodes=update_nodes))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_ControllerActor.check_nodes(controller))
    assert len(calls) == 2


def test_departed_node_deletes_its_replicas(monkeypatch):
    """Purging kills what ran on the node, HOT and cached alike."""
    live = [_node("10.0.0.1")]
    monkeypatch.setattr(cluster_mod, "list_nodes", lambda **kw: list(live))

    c = Cluster()
    c.update_nodes()
    node = c.nodes["id-10.0.0.1"]
    deleted = []
    node.deployments["model"] = {"a": SimpleNamespace(delete=lambda: deleted.append("hot"))}
    node.cache["model"] = {"b": SimpleNamespace(delete=lambda: deleted.append("cached"))}

    live.clear()
    c.update_nodes()

    assert c.nodes == {}
    assert deleted == ["hot", "cached"]


def test_controller_starts_despite_a_failed_sync(monkeypatch):
    """Ray doesn't retry a failed ``__init__``, even with ``max_restarts=-1``, so
    letting this error out would leave no controller until the next redeploy."""

    class FailingCluster:
        def __init__(self, **kwargs):
            pass

        def update_nodes(self):
            raise RuntimeError("the state API is unavailable")

    monkeypatch.setattr(controller_mod, "Cluster", FailingCluster)

    async def construct():
        return _ControllerActor(
            deployments=[],
            model_import_path="model",
            controller_import_path="controller",
            default_execution_timeout_seconds=60,
            model_cache_percentage=0.5,
            minimum_deployment_time_seconds=0,
            default_padding_factor=0.5,
            default_padding_bias=0,
        )

    assert isinstance(asyncio.run(construct()), _ControllerActor)


def test_status_keeps_live_replicas_past_the_actor_limit(monkeypatch):
    """Ray keeps a DEAD record for every actor it has run and cuts the list at
    ``limit`` silently. A live replica pushed off it vanishes from status, and
    reconcile stacks a second copy of a pinned model it can no longer see."""
    key = 'stub.Model:{"repo_id": "stub/m", "revision": null, "task": "text-generation"}'

    def deployment(replica_id):
        return SimpleNamespace(
            name=f"{replica_id}:ModelActor:{key}",
            model_key=key,
            replica_id=replica_id,
            deployment_level=SimpleNamespace(name="HOT"),
            pinned=True,
            dtype=None,
            execution_timeout_seconds=None,
            size_bytes=1,
            actor_class=None,
            gpus=None,
        )

    live, restarting, gone = deployment("zlive"), deployment("zrestart"), deployment("zgone")
    records = [
        SimpleNamespace(name=f"dead{i}:ModelActor:{key}", state="DEAD") for i in range(3000)
    ]
    records += [
        SimpleNamespace(name=live.name, state="ALIVE"),
        SimpleNamespace(name=restarting.name, state="RESTARTING"),
        SimpleNamespace(name=gone.name, state="DEAD"),
    ]

    def fake_list_actors(address, limit, filters=()):
        excluded = {value for key_, op, value in filters if key_ == "state" and op == "!="}
        kept = [r for r in records if r.state not in excluded]
        return sorted(kept, key=lambda r: r.name)[:limit]

    monkeypatch.setattr(controller_mod, "list_actors", fake_list_actors)
    monkeypatch.setattr(controller_mod, "get_downloaded_models", lambda: [])

    node = SimpleNamespace(
        iter_deployments=lambda: [(key, d.replica_id, d) for d in (live, restarting, gone)],
        iter_cache=lambda: [],
        gpu_resources=SimpleNamespace(gpus=[]),
        deployments={},
    )
    entry = SimpleNamespace(
        config=SimpleNamespace(to_json_string=lambda: "{}"), revision=None, n_params=1
    )
    controller = SimpleNamespace(
        cluster=SimpleNamespace(
            nodes={"id": node}, evaluator=SimpleNamespace(cache={key: entry})
        ),
        minimum_deployment_time_seconds=None,
    )

    deployments = _ControllerActor.status(controller)["deployments"]

    assert deployments[live.name]["application_state"] == "RUNNING"
    assert deployments[restarting.name]["application_state"] == "DEPLOYING"
    # Tracked with no live actor: surfaced, not silently dropped.
    assert deployments[gone.name]["application_state"] == "UNHEALTHY"
    assert deployments[gone.name]["deployment_level"] == "HOT"
    # Dead actors the controller no longer tracks don't show up at all.
    assert len(deployments) == 3


def test_all_nodes_departing_empties_the_registry(monkeypatch):
    """The outage shape: every GPU node dies at once, leaving a CPU-only head.

    The controller must report zero capacity rather than eight phantom nodes,
    since that is what /status and the down alert are both derived from.
    """
    live = [_node(f"10.0.0.{i}") for i in range(8)]
    monkeypatch.setattr(cluster_mod, "list_nodes", lambda **kw: list(live))

    c = Cluster()
    c.update_nodes()
    assert len(c.nodes) == 8

    live.clear()
    c.update_nodes()
    assert c.nodes == {}


@pytest.mark.parametrize(
    "err, expected",
    [
        # Exactly what nnsight's RemoteException stringified to during the
        # outage: a rendered traceback whose message is the *last* line.
        (
            "Traceback (most recent call last):\n\n"
            "RemoteException: Error submitting request to model deployment. "
            "Please try again later. Sorry for the inconvenience.",
            "RemoteException: Error submitting request to model deployment. "
            "Please try again later. Sorry for the inconvenience.",
        ),
        ('Traceback (most recent call last):\n  File "x.py", line 1, in f\n'
         "    boom()\nValueError: bad thing", "ValueError: bad thing"),
        ("Exceeded 60s timeout", "Exceeded 60s timeout"),
        ("Connection refused", "Connection refused"),
        ("Something failed\n  extra detail", "Something failed"),
        (None, "unknown"),
        ("", "unknown"),
    ],
)
def test_summarize_error(err, expected):
    assert summarize_error(err) == expected


def test_summarize_error_never_returns_the_bare_header():
    """The regression itself: line 0 of a traceback is never the message."""
    assert summarize_error(
        "Traceback (most recent call last):\n\nRemoteException: boom"
    ) != "Traceback (most recent call last):"
