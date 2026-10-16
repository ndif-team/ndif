"""Forked runners (``NDIF_SANDBOX_FORK=1``): what a pool hands out is still one
fresh, separately tracked process per request.

No server, no Ray and no weights: pools are built without a model key, so the
zygote and its children skip the meta-model build. Each test checks one property
a spawned runner already has and a forked one must keep: it is its own process,
it draws its own random numbers, ``stop()`` ends it, it dies with its parent,
and a broken zygote degrades to spawning instead of failing the request.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

if not sys.platform.startswith("linux"):
    pytest.skip("pidfds are Linux-only", allow_module_level=True)

torch = pytest.importorskip("torch")

from ndif.services.ray.sandbox import host, runner


def parent_of(pid: int) -> int:
    return int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])


def alive(pid: int) -> bool:
    return os.path.exists(f"/proc/{pid}")


@pytest.fixture
def fork_pool(monkeypatch):
    monkeypatch.setenv("NDIF_SANDBOX_FORK", "1")
    pool = host.Pool(size=2)
    yield pool
    pool.close()


def test_runners_are_separate_children_of_the_zygote(fork_pool):
    first = fork_pool.acquire(timeout=60)
    second = fork_pool.acquire(timeout=60)
    try:
        zygote = fork_pool.zygote.process.pid
        for sandbox in (first, second):
            assert isinstance(sandbox.process, host.ForkedProcess)
            assert parent_of(sandbox.process.pid) == zygote
            sandbox.connection(timeout=5).close()  # it serves
        assert first.process.pid != second.process.pid
    finally:
        first.stop()
        second.stop()


def test_stop_ends_a_forked_runner_and_removes_its_socket(fork_pool):
    sandbox = fork_pool.acquire(timeout=60)
    pid = sandbox.process.pid
    sandbox.stop()
    assert sandbox.process.poll() is not None
    assert not os.path.exists(sandbox.path)
    deadline = time.time() + 5
    while alive(pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not alive(pid)


def test_forked_runners_die_with_the_zygote(fork_pool):
    sandboxes = [fork_pool.acquire(timeout=60) for _ in range(2)]
    pids = [s.process.pid for s in sandboxes]
    os.kill(fork_pool.zygote.process.pid, signal.SIGKILL)
    deadline = time.time() + 3 * runner._PARENT_POLL_S + 2
    while any(alive(p) for p in pids) and time.time() < deadline:
        time.sleep(0.1)
    assert not any(alive(p) for p in pids)


def test_after_fork_reseeds_each_child(monkeypatch):
    """Two children of one seeded parent must not repeat each other's draws."""
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    torch.manual_seed(1234)
    draws = []
    parent = os.getpid()
    for _ in range(2):
        read, write = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read)
            runner._after_fork(parent)
            os.write(write, repr(torch.rand(4).tolist()).encode())
            os._exit(0)
        os.close(write)
        draws.append(os.read(read, 4096))
        os.close(read)
        os.waitpid(pid, 0)
    assert draws[0] != draws[1]


def test_a_dead_zygote_falls_back_to_spawning(fork_pool):
    fork_pool.zygote.process.kill()
    fork_pool.zygote.process.wait()
    sandbox = fork_pool._spawn()
    try:
        assert not isinstance(sandbox.process, host.ForkedProcess)
        sandbox.connection(timeout=5).close()
    finally:
        sandbox.stop()
