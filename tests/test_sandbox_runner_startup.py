"""What a runner pays before and after a request, off the request's path.

The meta-model build runs offline when the actor's task is known, so a runner
makes no Hub requests; and discarding a request's runner signals it without
holding the replica while the process exits. No server, Ray cluster or weights.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

pytest.importorskip("torch")
pytest.importorskip("nnsight")

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")

# Runs in a child: importing `sandbox.nns` installs the runner's IPC patches,
# which must not leak into the test process.
_BUILD = textwrap.dedent(
    """
    import sys
    from huggingface_hub import constants as hub
    from nnsight.modeling.mixins.remotable import Remotable
    from ndif.services.ray.sandbox import nns

    calls = []
    offline_works = sys.argv[2] == "1"

    class Meta:
        def _remoteable_persistent_objects(self):
            return {"Module:decoder_blocks.0": object()}

    def build(key, **kwargs):
        calls.append((kwargs.get("task"), hub.HF_HUB_OFFLINE))
        if hub.HF_HUB_OFFLINE and not offline_works:
            raise OSError("not in the cache")
        return Meta()

    Remotable.from_model_key = build
    task = None if sys.argv[1] == "-" else sys.argv[1]
    nns.load_meta_model('{"repo_id": "acme/tiny"}', task=task)
    assert "Module:decoder_blocks.0" in nns.PERSISTENT_OBJECTS
    print(calls, hub.HF_HUB_OFFLINE)
    """
)


def build_calls(task: str, offline_works: bool) -> str:
    env = {**os.environ, "PYTHONPATH": _SRC, "HF_HUB_OFFLINE": "0"}
    result = subprocess.run(
        [sys.executable, "-c", _BUILD, task, "1" if offline_works else "0"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_meta_build_with_a_task_runs_offline_only():
    assert build_calls("image-classification", True) == (
        "[('image-classification', True)] False"
    )


def test_meta_build_falls_back_online_when_offline_fails():
    assert build_calls("text-generation", False) == (
        "[('text-generation', True), (None, False)] False"
    )


def test_meta_build_without_a_task_stays_online():
    assert build_calls("-", True) == "[(None, False)] False"


def test_discard_returns_before_the_runner_exits(tmp_path):
    pytest.importorskip("ray")
    from ndif.services.ray.sandbox.host import Sandbox
    from ndif.services.ray.sandbox.model import SandboxHost

    # A runner whose SIGTERM handler takes a second, like CUDA teardown.
    process = subprocess.Popen([
        sys.executable, "-c",
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, lambda *_: (time.sleep(1), sys.exit(0)))\n"
        "print(flush=True); time.sleep(60)",
    ], stdout=subprocess.PIPE)
    process.stdout.readline()  # handler installed
    socket_path = tmp_path / "runner.sock"
    socket_path.touch()
    actor = SandboxHost.__new__(SandboxHost)
    actor.execution_sandbox = Sandbox(str(socket_path), process)

    started = time.time()
    actor.discard_sandbox()
    assert time.time() - started < 0.5
    assert actor.execution_sandbox is None
    assert process.poll() is None  # still in its handler

    assert process.wait(timeout=10) == 0  # the signal was sent
    deadline = time.time() + 5
    while socket_path.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not socket_path.exists()


def test_a_runner_whose_parent_dies_during_imports_exits(tmp_path):
    """The parent exits while the runner is still importing torch and nnsight,
    the window in which reading ``getppid`` would have recorded init."""
    sock = tmp_path / "orphan.sock"
    launcher = textwrap.dedent(
        f"""
        import os, subprocess, sys
        p = subprocess.Popen(
            [sys.executable, "-m", "ndif.services.ray.sandbox.runner",
             {str(sock)!r}, "1", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(p.pid, flush=True)
        """
    )
    env = {**os.environ, "PYTHONPATH": _SRC}
    out = subprocess.run(
        [sys.executable, "-c", launcher],
        capture_output=True, text=True, env=env, timeout=60,
    )
    pid = int(out.stdout.split()[0])
    deadline = time.time() + 60
    while os.path.exists(f"/proc/{pid}") and time.time() < deadline:
        time.sleep(0.1)
    alive = os.path.exists(f"/proc/{pid}")
    if alive:
        os.kill(pid, 9)
    assert not alive
    assert not sock.exists()  # it exited before binding
