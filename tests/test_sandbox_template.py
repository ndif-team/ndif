"""Runners forked from a template, and runners that outlive their parent.

A pool forks its runners from one ``host.Template`` instead of starting each from
nothing. A forked runner has to behave like a started one: serve a request
against the meta model it inherited, draw random numbers of its own, and die
when it is stopped or its template goes away.

No server and no Ray: a real gpt2 on CPU and real runner processes, as in
``test_sandbox_iter_pins.py``.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from test_sandbox_iter_pins import (  # noqa: F401  (model is a fixture)
    PROMPT,
    Capture,
    model,
)

from ndif.services.ray.sandbox import host
from ndif.services.ray.sandbox.driver import SandboxDriver
from ndif.services.ray.sandbox.messages import Payload


def run(model, sandbox, blob):
    connection = sandbox.connection()
    try:
        connection.send(Payload(blob, False, "torch.float32", None, {}))
        data, _ = SandboxDriver(model, torch.float32).pump(connection)
    finally:
        connection.close()
    return torch.load(io.BytesIO(data), weights_only=False)


def gone_within(pid, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not host.running(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(scope="module")
def template(model):
    template = host.Template(model_key=model.to_model_key())
    yield template
    template.close()


def test_a_forked_runner_serves_a_request(model, template):
    with model.trace(PROMPT):
        hidden = model.transformer.h[5].output.save()
    local = hidden

    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        hidden = model.transformer.h[5].output.save()

    sandbox = template.fork()
    try:
        saved = run(model, sandbox, capture.blob)
    finally:
        sandbox.stop()
    assert torch.equal(saved["hidden"], local)


def test_forked_runners_draw_their_own_numbers(model, template):
    # A fork starts with a copy of the template's random state.
    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        drawn = torch.rand(4).save()

    draws = []
    for _ in range(2):
        sandbox = template.fork()
        try:
            draws.append(run(model, sandbox, capture.blob)["drawn"])
        finally:
            sandbox.stop()
    assert not torch.equal(draws[0], draws[1])


def test_stop_kills_a_forked_runner(template):
    sandbox = template.fork()
    assert host.running(sandbox.process)
    sandbox.stop()
    assert gone_within(sandbox.process, 5)
    assert not os.path.exists(sandbox.path)


def test_closing_the_template_ends_its_runners(model):
    template = host.Template(model_key=model.to_model_key())
    sandbox = template.fork()
    template.close()
    assert gone_within(sandbox.process, 5)


def test_a_runner_whose_parent_is_already_gone_exits():
    # The pid a runner is given is the parent it watches. Read from getppid()
    # after the imports instead, a parent that died during them had already
    # been replaced by init, which the runner then watched forever.
    path = host.socket_path()
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    runner = subprocess.Popen(
        [sys.executable, "-m", "ndif.services.ray.sandbox.runner",
         path, "1", str(gone.pid), "runner"],
        env={**host.runner_env(), "PYTHONPATH": host._PACKAGE_ROOT},
    )
    try:
        assert runner.wait(timeout=60) == 1
    finally:
        runner.kill()
        if os.path.exists(path):
            os.remove(path)
