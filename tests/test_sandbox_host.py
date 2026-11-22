"""The actor's half of a sandboxed request, without the actor.

The other harness files spawn a runner and pump it with a ``SandboxDriver``
themselves. That leaves out what the model actor does around it:
``SandboxHost.run_in_runner`` takes a warm runner from the pool, sends the
request as a ``Payload``, and hands the block's prints to the client. Only the
live-server suites reached that code.

Here a ``SandboxModelDeployment`` is built without its ``__init__`` (which loads a
model and needs Ray's cluster), given a real gpt2 on CPU and a real pool of one
runner, and asked to run a request. No server. The deployment class lives behind
``import ray``, so this skips where ray is not installed.
"""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")
pytest.importorskip("ray")

from test_sandbox_iter_pins import (  # noqa: F401  (model is a fixture)
    PROMPT,
    Capture,
    model,
)
from test_sandbox_template import gone_within


def deployment_for(model):
    """A ``SandboxModelDeployment`` around ``model``, with a pool of one runner."""
    from ndif.services.ray.sandbox.model import SandboxModelDeployment

    deployment = object.__new__(SandboxModelDeployment)
    deployment.model = model
    deployment.dtype = torch.float32
    deployment.model_key = model.to_model_key()
    deployment.open_pool(pool_size=1)
    return deployment


def request_for(blob, responses):
    return SimpleNamespace(
        payload=blob,
        compress=False,
        env={},
        respond=lambda status, text: responses.append((status, text)),
    )


def test_a_request_runs_through_the_pool_and_its_prints_reach_the_client(model):
    from nnsight.schema.response import Status

    with model.trace(PROMPT):
        hidden = model.transformer.h[5].output.save()
    local = hidden

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        print("from the block")
        hidden = model.transformer.h[5].output.save()

    deployment = deployment_for(model)
    responses = []
    try:
        data, deserialize_ms = deployment.run_in_runner(
            request_for(capture.blob, responses), None
        )
    finally:
        deployment.discard_sandbox()
        deployment.close()

    saved = torch.load(io.BytesIO(data), weights_only=False)
    assert torch.allclose(saved["hidden"], local)
    assert deserialize_ms is not None
    assert (Status.LOG, "from the block") in responses


def test_a_parked_replica_keeps_no_runners_and_serves_again_once_restored(
    model, monkeypatch
):
    from ndif.services.ray.deployments.modeling.base import BaseModelDeployment

    # The base's half moves the weights between the card and the CPU, which is
    # not what this is about.
    async def park(self):
        self.cached = True

    monkeypatch.setattr(BaseModelDeployment, "to_cache", park)
    monkeypatch.setattr(BaseModelDeployment, "from_cache", lambda self, gpus: None)

    with model.trace(PROMPT):
        hidden = model.transformer.h[5].output.save()
    local = hidden

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        hidden = model.transformer.h[5].output.save()

    deployment = deployment_for(model)
    try:
        parked = deployment.pool
        waiting = parked.ready.get(timeout=60)
        parked.ready.put(waiting)
        template = parked.template.server.process.pid

        asyncio.run(deployment.to_cache())
        assert gone_within(waiting.process, 5)
        assert gone_within(template, 5)
        with pytest.raises(RuntimeError, match="closed"):
            parked.acquire()

        deployment.from_cache({})
        assert deployment.pool is not parked
        data, _ = deployment.run_in_runner(request_for(capture.blob, []), None)
    finally:
        deployment.discard_sandbox()
        deployment.close()

    saved = torch.load(io.BytesIO(data), weights_only=False)
    assert torch.allclose(saved["hidden"], local)
