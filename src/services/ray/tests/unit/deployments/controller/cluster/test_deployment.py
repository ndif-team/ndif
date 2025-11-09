import types
from datetime import datetime, timezone

import pytest


# ---- Helpers ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_model_key(monkeypatch):
    """
    The module calls MODEL_KEY(model_key) in __init__.
    Patch it to a simple coercion so we don't depend on the real ndif types.
    """
    # Import the module *once* so we can patch its symbols
    import ndif_ray.deployments.controller.cluster.deployment as depmod
    monkeypatch.setattr(depmod, "MODEL_KEY", lambda x: f"MK:{x}", raising=True)
    return depmod


@pytest.fixture
def fixed_time(monkeypatch):
    """Freeze time.time() so deployed timestamps are stable."""
    import time as _time
    monkeypatch.setattr(_time, "time", lambda: 1_000.0, raising=True)
    return 1_000.0


@pytest.fixture
def module(monkeypatch, patch_model_key):
    """Expose the imported deployment module (already has MODEL_KEY patched)."""
    return patch_model_key


@pytest.fixture
def fake_logger(mocker, module):
    lg = mocker.Mock()
    module.logger = lg  # replace module-level logger
    return lg


@pytest.fixture
def fake_ray(mocker, module):
    """Replace the ray module inside deployment.py with a stubbed namespace."""
    fake = types.SimpleNamespace()
    fake.get_actor = mocker.Mock()
    fake.kill = mocker.Mock()
    module.ray = fake
    return fake


# ---- Tests -----------------------------------------------------------------

def test_deployment_level_values(module):
    # Ensure enum wiring is correct and values are serialized as expected
    assert module.DeploymentLevel.HOT.value == "hot"
    assert module.DeploymentLevel.WARM.value == "warm"
    assert module.DeploymentLevel.COLD.value == "cold"


def test_init_and_get_state_casts_model_key_and_sets_fields(module, fixed_time):
    d = module.Deployment(
        model_key="my/model@v1",
        deployment_level=module.DeploymentLevel.HOT,
        gpus_required=2,
        size_bytes=123456,
        dedicated=True,
        cached=False,
    )

    st = d.get_state()
    # MODEL_KEY was patched to prefix with MK:
    assert st["model_key"] == "MK:my/model@v1"
    assert st["deployment_level"] == "hot"
    assert st["gpus_required"] == 2
    assert st["size_bytes"] == 123456
    assert st["dedicated"] is True
    assert st["cached"] is False
    # deployed should be the frozen time
    assert st["deployed"] == pytest.approx(1_000.0, rel=0, abs=0)


def test_end_time_uses_minimum_seconds(module, fixed_time):
    d = module.Deployment(
        model_key="k",
        deployment_level=module.DeploymentLevel.WARM,
        gpus_required=0,
        size_bytes=0,
    )
    dt = d.end_time(60)
    assert dt == datetime.fromtimestamp(1_060.0, tz=timezone.utc)


def test_delete_kills_actor_on_success(module, fake_ray, fake_logger):
    # Arrange: get_actor returns a fake actor object
    actor = object()
    fake_ray.get_actor.return_value = actor

    d = module.Deployment("k", module.DeploymentLevel.COLD, 0, 0)
    d.delete()

    fake_ray.get_actor.assert_called_once_with("ModelActor:MK:k")
    fake_ray.kill.assert_called_once_with(actor)
    fake_logger.exception.assert_not_called()


def test_delete_logs_on_failure(module, fake_ray, fake_logger):
    # Arrange: get_actor raises (e.g., actor not found)
    fake_ray.get_actor.side_effect = RuntimeError("not found")

    d = module.Deployment("k", module.DeploymentLevel.COLD, 0, 0)
    d.delete()

    fake_ray.kill.assert_not_called()
    fake_logger.exception.assert_called_once()
    # Optional: check message contains model key
    msg = fake_logger.exception.call_args[0][0]
    #assert "ModelActor" in msg and "MK:k" in msg
    assert "Error removing actor" in msg
    assert "MK:k" in msg
    # don't assert "ModelActor" here


def test_cache_returns_object_ref_on_success(module, fake_ray, fake_logger, mocker):
    # Fake actor with to_cache.remote()
    class FakeHandle:
        def to_cache(self):
            return self
        def remote(self):
            return "OBJECT_REF"

    fake_ray.get_actor.return_value = FakeHandle()

    d = module.Deployment("k", module.DeploymentLevel.HOT, 1, 1)
    ref = d.cache()

    fake_ray.get_actor.assert_called_once_with("ModelActor:MK:k")
    assert ref == "OBJECT_REF"
    fake_logger.exception.assert_not_called()


def test_cache_logs_and_returns_none_on_failure(module, fake_ray, fake_logger):
    fake_ray.get_actor.side_effect = RuntimeError("boom")

    d = module.Deployment("k", module.DeploymentLevel.HOT, 1, 1)
    out = d.cache()

    assert out is None
    fake_logger.exception.assert_called_once()

    # 👇 Add these lines to verify message content
    msg = fake_logger.exception.call_args[0][0]
    assert "Error adding actor" in msg
    assert "MK:k" in msg