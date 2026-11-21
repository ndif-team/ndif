"""Two hosts drive one runner, as the ranks of a tensor-parallel model do.

The runner holds one set of workers and talks to every host through a
``Fanout``: each message it sends reaches both, and it waits for both before it
acts on the first host's. The second host connects as a follower, which sends
the type of each message and no payload. ``test_fanout.py`` checks that barrier
with scripted peers. This runs a real block through it: two copies of gpt2, each
with its own ``SandboxDriver``, so every message of a run crosses the fan-out,
including the write-back of an in-place edit and a ``tracer.cache()``.

The copies are identical, where real ranks hold different shards, so the value
the runner takes from the first host is the whole value and the trusted run is
the ground truth. No server, no Ray and no GPU.
"""

from __future__ import annotations

import io
import threading

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from ndif.services.ray.sandbox import host
from ndif.services.ray.sandbox.driver import SandboxDriver
from ndif.services.ray.sandbox.messages import Payload

from test_sandbox_iter_pins import PROMPT, REPO, Capture


@pytest.fixture(scope="module")
def models():
    """Two copies of gpt2 on CPU, served the way the actor serves one: no
    parameter requires grad."""
    from nnsight import TransformersModel

    copies = []
    for _ in range(2):
        try:
            copy = TransformersModel(REPO, task="text-generation", device="cpu", dispatch=True)
        except Exception as error:  # no cached weights and no network, most likely
            pytest.skip(f"could not load {REPO}: {error}")
        copy._module.requires_grad_(False)
        copies.append(copy)
    return copies


def run_on_two_hosts(models, blob: bytes):
    """Drive ``blob`` through one runner from two hosts; return what each got back."""
    leader_model, follower_model = models
    sandbox = host.spawn(model_key=leader_model.to_model_key(), peers=2)
    results, errors = {}, []

    def drive(name, model, connection):
        try:
            data, _ = SandboxDriver(model, torch.float32).pump(connection)
            results[name] = torch.load(io.BytesIO(data), weights_only=False)
        except BaseException as error:  # surfaced by the caller
            errors.append(error)

    try:
        # The first host to connect is the one whose payload the runner reads.
        leader = sandbox.connection()
        follower = host.FollowerConnection.open(sandbox.path)
        leader.send(Payload(blob, False, "torch.float32", None, {}))
        thread = threading.Thread(target=drive, args=("follower", follower_model, follower))
        thread.start()
        drive("leader", leader_model, leader)
        thread.join(timeout=120)
    finally:
        sandbox.stop()
    if errors:
        raise errors[0]
    return results


def test_a_block_with_an_edit_and_a_cache(models):
    model = models[0]
    norm = model.transformer.h[5].ln_1

    with model.trace(PROMPT) as tracer:
        cache = tracer.cache(modules=[model.transformer.h[2]]).save()
        norm.input[:] = 0
        logits = model.lm_head.output[0, -1].save()
    local, local_cache = logits, cache["model.transformer.h.2"].output

    capture = Capture()
    with model.trace(PROMPT, backend=capture) as tracer:
        cache = tracer.cache(modules=[model.transformer.h[2]]).save()
        norm.input[:] = 0
        logits = model.lm_head.output[0, -1].save()

    results = run_on_two_hosts(models, capture.blob)

    assert set(results) == {"leader", "follower"}, "a host never finished"
    for name, saved in results.items():
        assert torch.allclose(saved["logits"], local), f"{name} differs from the trusted run"
        got = saved["cache"]["model.transformer.h.2"].output
        assert torch.allclose(got, local_cache), f"{name}'s cache differs"


def test_a_generate_with_a_stop(models):
    model = models[0]

    with model.generate(PROMPT, max_new_tokens=3, min_new_tokens=3, do_sample=False) as tracer:
        hidden = model.transformer.h[5].output.save()
        tracer.stop()
    local = hidden

    capture = Capture()
    with model.generate(PROMPT, backend=capture, max_new_tokens=3, min_new_tokens=3, do_sample=False) as tracer:
        hidden = model.transformer.h[5].output.save()
        tracer.stop()

    results = run_on_two_hosts(models, capture.blob)

    assert set(results) == {"leader", "follower"}, "a host never finished"
    for name, saved in results.items():
        assert torch.allclose(saved["hidden"], local), f"{name} differs from the trusted run"
