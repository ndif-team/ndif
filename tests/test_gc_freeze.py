"""Loaded-model objects are frozen out of the per-request ``gc.collect``.

``cleanup()`` runs a full ``gc.collect`` after every request. Before the freeze
it re-scanned every object of the loaded model each time. ``freeze_loaded_objects``
collects what is already garbage, then moves the survivors to the permanent
generation, which ``gc.collect`` and ``gc.get_objects`` skip.
"""

from __future__ import annotations

import gc
import weakref

import pytest
import torch

from ndif.services.ray.deployments.modeling.base import freeze_loaded_objects


@pytest.fixture(autouse=True)
def unfreeze():
    yield
    gc.unfreeze()


class Node:
    pass


def test_survivors_frozen_and_prior_garbage_collected():
    model = torch.nn.Sequential(torch.nn.Linear(4, 4), torch.nn.ReLU())

    cycle = Node()
    cycle.self = cycle
    dead = weakref.ref(cycle)
    del cycle

    freeze_loaded_objects()

    assert dead() is None
    tracked = {id(o) for o in gc.get_objects()}
    assert id(model) not in tracked
    assert id(model[0]) not in tracked
    assert id(model[0]._parameters) not in tracked


def test_objects_created_after_freeze_are_still_collected():
    freeze_loaded_objects()

    cycle = Node()
    cycle.self = cycle
    dead = weakref.ref(cycle)
    del cycle
    gc.collect()

    assert dead() is None
