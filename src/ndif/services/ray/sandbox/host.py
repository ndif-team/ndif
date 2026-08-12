"""Host side of the sandbox: spawn runner processes and talk to them.

A ``Sandbox`` is a running runner process reachable over its Unix socket. ``Pool``
keeps a set of runners pre-warmed in the background and hands out a *fresh* one
per request (which the caller then ``stop()``s), so nothing leaks between
requests while acquiring one still avoids the python-startup cost.
"""

import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid

from .protocol import Channel, unpack

# The dir containing the top-level ``ndif`` package, so the runner (which runs as
# ndif.services.ray.sandbox.runner) can import ndif.* and reuse its helpers.
# sandbox -> ray -> services -> ndif -> <root>.
_SANDBOX_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(_SANDBOX_DIR)))
)


class Connection(Channel):
    """The host's end of a runner channel.

    Inherits the framing; overrides only the receive codec, because the two
    directions deliberately differ — the host sends one value, the runner replies
    with an event name plus values and kwargs (see this module's protocol).
    """

    @classmethod
    def open(cls, path: str, timeout: float = 60.0) -> "Connection":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        sock.settimeout(None)
        return cls(sock)

    def recv(self, timeout=None):
        """Receive a runner message as ``(values, kwargs)``."""
        return unpack(self.recv_raw(timeout))


class Sandbox:
    """A running runner process, reachable over its Unix socket."""

    def __init__(self, path: str, process: subprocess.Popen):
        self.path = path
        self.process = process

    def connection(self, timeout: float = 60.0) -> Connection:
        return Connection.open(self.path, timeout)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def spawn(
    python: str = sys.executable,
    quiet: bool = True,
    timeout: float = 30.0,
    peers: int = 1,
) -> Sandbox:
    """Launch a runner process and wait until its socket is accepting.

    ``peers`` is how many hosts will drive each request. Above one they are
    ranks of one model and the runner waits for all of them before it starts
    (see `Runner`); the runner has to be told at launch because it accepts the
    connections before anyone has said anything.
    """
    path = f"/tmp/sbx-{uuid.uuid4().hex[:12]}.sock"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [_PACKAGE_ROOT, env.get("PYTHONPATH", "")])
    )
    # See the note in tp/common.rank_env: a runner is another process whose hash
    # randomization would make a block's set/dict iteration order differ from the
    # host's, and — under tensor parallelism, where every rank hosts the same runner — from
    # its peers'.
    env["PYTHONHASHSEED"] = "0"
    output = subprocess.DEVNULL if quiet else None
    process = subprocess.Popen(
        [python, "-m", "ndif.services.ray.sandbox.runner", path, str(peers)],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=output,
        stderr=output,
    )

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.connect(path)
                probe.close()
                return Sandbox(path, process)
            except OSError:
                pass
        if process.poll() is not None:
            raise RuntimeError("runner process exited before its socket was ready")
        time.sleep(0.02)
    process.kill()
    raise TimeoutError("timed out waiting for the runner socket")


class Pool:
    """A warm pool that hands out one *fresh* runner per request.

    Each request gets its own runner process and stops it when done, so nothing —
    compiled trace blocks, globals, module state — leaks between requests (the
    isolation the sandbox is for). The pool keeps up to ``size`` runners pre-warmed
    in the background so acquiring one doesn't pay python/nnsight startup; taking
    one triggers a refill. Callers must ``stop()`` what they acquire — there's no
    release-back-to-pool.
    """

    def __init__(
        self,
        size: int = 2,
        python: str = sys.executable,
        quiet: bool = True,
        peers: int = 1,
    ):
        self.size = size
        self.python = python
        self.quiet = quiet
        self.peers = peers
        self.ready: "queue.Queue" = queue.Queue()
        self.lock = threading.Lock()
        self.spawning = 0
        self.closed = False
        for _ in range(size):
            self.refill()

    def refill(self) -> None:
        """Warm one runner into the ready set in the background, unless the pool is
        already at ``size`` (counting in-flight spawns)."""
        with self.lock:
            if self.closed or self.ready.qsize() + self.spawning >= self.size:
                return
            self.spawning += 1

        def warm():
            try:
                sandbox = spawn(python=self.python, quiet=self.quiet, peers=self.peers)
            finally:
                with self.lock:
                    self.spawning -= 1
            if self.closed:
                sandbox.stop()
            else:
                self.ready.put(sandbox)

        threading.Thread(target=warm, daemon=True).start()

    def acquire(self, timeout: float = 30.0) -> Sandbox:
        """Take a warm runner (or spawn one if the pool is drained) and top the
        pool back up. The caller owns the returned runner and must ``stop()`` it."""
        try:
            sandbox = self.ready.get(timeout=timeout)
        except queue.Empty:
            sandbox = spawn(python=self.python, quiet=self.quiet, peers=self.peers)
        self.refill()
        return sandbox

    def close(self) -> None:
        with self.lock:
            self.closed = True
        while True:
            try:
                self.ready.get_nowait().stop()
            except queue.Empty:
                break
