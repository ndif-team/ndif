"""Regression tests for the two bugs behind the 2026-09-08 prod outage.

A slurm walltime kill removed every GPU worker at once. Neither bug caused the
outage, but together they made it silent for an hour: the controller never
noticed the nodes were gone, so ``/status`` kept advertising a full cluster and
the dashboard's down alert never fired, while the only notification that did go
out — the model-trace ping — said nothing but "Traceback (most recent call
last):" for all 23 models.
"""

import sys
from types import SimpleNamespace

import pytest

from ndif.services.ray.deployments.controller.cluster import cluster as cluster_mod
from ndif.services.ray.deployments.controller.cluster.cluster import Cluster
from ndif.services.dashboard.jobs.util import summarize_error


def _node(ip, gpus=4):
    """A NodeState stand-in. Ray reports DEAD nodes with resources intact, so
    these deliberately carry full ``resources_total`` regardless of state."""
    return SimpleNamespace(
        node_id=f"id-{ip}",
        node_name=ip,
        labels={"ray.io/accelerator-type": "A40"},
        resources_total={
            "GPU": float(gpus),
            "cuda_memory_bytes": 47699722240.0 * gpus,
            "cpu_memory_bytes": 248423067648.0,
        },
    )


def test_update_nodes_requests_alive_nodes_only(monkeypatch):
    """The ALIVE filter must be pushed to Ray, not applied after the fact.

    Ray's ``list_nodes`` returns DEAD nodes with ``"GPU"`` still in
    ``resources_total``, so any client-side filter on resources re-registers
    them forever.
    """
    seen = {}

    def fake_list_nodes(**kwargs):
        seen.update(kwargs)
        return [_node("10.0.0.1")]

    monkeypatch.setattr(cluster_mod, "list_nodes", fake_list_nodes)
    Cluster().update_nodes()

    assert ("state", "=", "ALIVE") in seen.get("filters", []), (
        "update_nodes must ask the GCS for ALIVE nodes; it got "
        f"filters={seen.get('filters')!r}"
    )


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
