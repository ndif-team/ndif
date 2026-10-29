"""The controller passes its own cluster's GCS address to Ray's state API and
bootstrap lookup, which do not reuse the calling process's connection.

No Ray: the runtime context is a fake with a fixed address and each Ray entry
point a fake that records what it was given.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import ray

from ndif.services.ray.deployments.controller import controller as controller_mod
from ndif.services.ray.deployments.controller.cluster import cluster as cluster_mod
from ndif.services.ray.deployments.controller.cluster.cluster import Cluster

OWN = "10.77.3.21:41999"


@pytest.fixture(autouse=True)
def own_cluster(monkeypatch):
    monkeypatch.setattr(
        ray, "get_runtime_context", lambda: SimpleNamespace(gcs_address=OWN)
    )


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
    # Past Ray's default of 100 entries (DEAD records included) live replicas
    # drop out of status.
    assert seen["limit"] == 2000


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
