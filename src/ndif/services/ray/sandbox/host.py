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
from typing import Optional, Tuple

from .protocol import Channel, signature_of, unpack

# The dir containing the top-level ``ndif`` package, so the runner (which runs as
# ndif.services.ray.sandbox.runner) can import ndif.* and reuse its helpers.
# sandbox -> ray -> services -> ndif -> <root>.
_SANDBOX_DIR = os.path.dirname(os.path.abspath(__file__))
_PACKAGE_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(_SANDBOX_DIR)))
)


# What a runner is allowed to inherit from the actor's environment.
#
# An allowlist rather than a copy, because the actor's environment is the
# operator's: measured on a live deployment it carried `HF_TOKEN` and
# `NDIF_INFLUX_TOKEN`, and a production one would add object-store credentials
# and `NDIF_POSTGRES_URL`. The runner exists to execute other people's Python,
# and `os.environ` is the first place to look.
#
# Each name here is something a runner genuinely needs:
#
# * the loader and locale basics, so python and its extensions start at all;
# * `CUDA_VISIBLE_DEVICES`, because the block computes on GPU and its device
#   numbering has to match the host it is driving -- under tensor parallelism a
#   device index is a position in this list, not a physical card;
# * cache locations, so a block that touches the Hub or torch.hub finds the
#   shared cache instead of downloading into the container;
# * thread-count knobs an operator tuned for the box.
#
# What it deliberately drops, beyond the credentials: `RANK`, `LOCAL_RANK`,
# `WORLD_SIZE`, `MASTER_ADDR` and `MASTER_PORT`. Rank 0 sets those on itself to
# join the process group, and a runner spawned from it used to inherit them and
# start life claiming to be rank 0 of a group it is not in -- which accelerate
# and transformers both read to decide they are in a distributed launch.
RUNNER_ENV: Tuple[str, ...] = (
    "PATH",
    "LD_LIBRARY_PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "CUDA_VISIBLE_DEVICES",
    "HF_HOME",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    "TORCH_HOME",
    "XDG_CACHE_HOME",
    "OMP_NUM_THREADS",
    "TOKENIZERS_PARALLELISM",
)


def runner_env() -> dict:
    """The environment a runner starts with: `RUNNER_ENV`, and nothing else."""
    return {
        name: os.environ[name] for name in RUNNER_ENV if name in os.environ
    }


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
        """Receive a runner message as ``(values, kwargs)``, on this host's device."""
        return unpack(self.recv_raw(timeout), self.map_location)


class FollowerConnection(Connection):
    """A host whose turn at the barrier matters but whose payload never does.

    [`Fanout`][ndif.services.ray.sandbox.protocol.Fanout] waits for every peer,
    compares signatures, and returns **peer 0's** message — so every other peer's
    payload is deserialized and dropped. Under tensor parallelism those payloads
    are whole activations, identical on every rank because the gather already made
    them whole, so at degree 8 seven eighths of this leg was tensors serialized,
    sent, rebuilt and discarded.

    This sends the signature alone. The barrier is untouched: still one message per
    event per peer, in the same order, so a peer that resumed a worker while
    another finished is still caught. What is given up is the *option* of ever
    comparing payloads across ranks — which was never taken, since the signature
    defaults to the event name and comparing whole tensors at every location would
    cost more than the interleave it guards.

    Every tensor-parallel shard is a follower by construction; rank 0 is the actor
    and always the leader, which is why this needs no rank plumbing.
    """

    def send(self, value) -> None:
        super().send((signature_of(value),))


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
    model_key: Optional[str] = None,
    trust_remote_code: bool = False,
) -> Sandbox:
    """Launch a runner process and wait until its socket is accepting.

    ``peers`` is how many hosts will drive each request. Above one they are
    ranks of one model and the runner waits for all of them before it starts
    (see `Runner`); the runner has to be told at launch because it accepts the
    connections before anyone has said anything.

    ``model_key`` is the checkpoint the runner builds its *meta* model from, so
    the payload's ``Module:<path>`` / ``Tokenizer`` ids resolve to weightless
    local objects rather than to ``None``. It is told at launch for the same
    reason ``peers`` is, and because the build is what the readiness wait is
    mostly waiting for. Omitted, the runner resolves those ids to ``None`` — the
    behaviour before meta models, and what a standalone runner gets.
    ``trust_remote_code`` is the actor's, since the build has to be the same one.
    """
    path = f"/tmp/sbx-{uuid.uuid4().hex[:12]}.sock"
    env = runner_env()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [_PACKAGE_ROOT, env.get("PYTHONPATH", "")])
    )
    # See the note in tp/common.rank_env: a runner is another process whose hash
    # randomization would make a block's set/dict iteration order differ from the
    # host's, and — under tensor parallelism, where every rank hosts the same runner — from
    # its peers'.
    env["PYTHONHASHSEED"] = "0"
    # `quiet` silences stdout only. **stderr is never discarded**: the block's own
    # output is forwarded as PRINT events (see nns.Writer), so anything reaching
    # this stderr is the runner process failing on its own account -- including
    # `serve`'s "runner: ..." handler, which is the only report of a request the
    # runner could not finish. Sending it to DEVNULL made a runner that died
    # mid-request indistinguishable from one that hung, and cost hours.
    argv = [python, "-m", "ndif.services.ray.sandbox.runner", path, str(peers)]
    if model_key is not None:
        argv += [model_key, "1" if trust_remote_code else "0"]
    process = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=None,
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
        model_key: Optional[str] = None,
        trust_remote_code: bool = False,
    ):
        self.size = size
        self.python = python
        self.quiet = quiet
        self.peers = peers
        self.model_key = model_key
        self.trust_remote_code = trust_remote_code
        self.ready: "queue.Queue" = queue.Queue()
        self.lock = threading.Lock()
        self.spawning = 0
        self.closed = False
        for _ in range(size):
            self.refill()

    def _spawn(self) -> Sandbox:
        """One runner, configured the way this pool's runners are."""
        return spawn(
            python=self.python,
            quiet=self.quiet,
            peers=self.peers,
            model_key=self.model_key,
            trust_remote_code=self.trust_remote_code,
        )

    def refill(self) -> None:
        """Warm one runner into the ready set in the background, unless the pool is
        already at ``size`` (counting in-flight spawns)."""
        with self.lock:
            if self.closed or self.ready.qsize() + self.spawning >= self.size:
                return
            self.spawning += 1

        def warm():
            try:
                sandbox = self._spawn()
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
            sandbox = self._spawn()
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
