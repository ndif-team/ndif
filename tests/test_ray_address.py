"""The controller names its own Ray cluster instead of letting Ray guess.

Ray's state API (``list_nodes``, ``list_actors``) and its bootstrap lookup do
not use the calling process's connection. Given no address they guess: on a host
where another Ray cluster's processes are visible, they raised "Found multiple
active Ray instances" and the controller died in ``__init__``. Each call site now
passes ``gcs_address()``, the cluster the controller runs in.

No Ray: ``gcs_address`` is swapped for a fixed address and each Ray entry point
for a fake that records what it was given.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ndif.services.ray.deployments.controller import controller as controller_mod
from ndif.services.ray.deployments.controller.cluster import cluster as cluster_mod
from ndif.services.ray.deployments.controller.cluster.cluster import Cluster

OWN = "10.77.3.21:41999"


@pytest.fixture(autouse=True)
def own_cluster(monkeypatch):
    monkeypatch.setattr(cluster_mod, "gcs_address", lambda: OWN)
    monkeypatch.setattr(controller_mod, "gcs_address", lambda: OWN)


def test_update_nodes_lists_nodes_of_own_cluster(monkeypatch):
    seen = {}

    def fake_list_nodes(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(cluster_mod, "list_nodes", fake_list_nodes)
    Cluster().update_nodes()

    assert seen["address"] == OWN


def test_status_lists_actors_of_own_cluster(monkeypatch):
    seen = {}

    class Recorded(Exception):
        pass

    def fake_list_actors(**kwargs):
        seen.update(kwargs)
        raise Recorded  # status() needs a whole controller past this point

    monkeypatch.setattr(controller_mod, "list_actors", fake_list_actors)
    with pytest.raises(Recorded):
        controller_mod._ControllerActor.status(SimpleNamespace())

    assert seen["address"] == OWN


def test_cluster_state_connects_to_own_gcs(monkeypatch):
    seen = []

    class FakeOptions:
        @staticmethod
        def create(address, *args, **kwargs):
            seen.append(address)
            return object()

    class FakeGlobalState:
        def _initialize_global_state(self, options):
            pass

    monkeypatch.setattr(cluster_mod, "GcsClientOptions", FakeOptions)
    monkeypatch.setattr(cluster_mod, "GlobalState", FakeGlobalState)
    Cluster().state

    assert seen == [OWN]
