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

from .protocol import Channel, signature_of

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
# * thread-count knobs an operator tuned for the box;
# * `NDIF_SANDBOX_PRELOAD_CUDA`, the operator's choice of whether a waiting
#   runner starts CUDA early (`runner.PRELOAD_CUDA`).
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
    "NDIF_SANDBOX_PRELOAD_CUDA",
)


def runner_env() -> dict:
    """The environment a runner starts with: `RUNNER_ENV`, and nothing else."""
    return {
        name: os.environ[name] for name in RUNNER_ENV if name in os.environ
    }


class Connection(Channel):
    """The host's end of a runner channel: a `Channel` that connects to the
    runner's socket."""

    @classmethod
    def open(cls, path: str, timeout: float = 60.0) -> "Connection":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        sock.settimeout(None)
        return cls(sock)


class FollowerConnection(Connection):
    """A host whose turn at the barrier matters but whose payload never does.

    [`Fanout`][ndif.services.ray.sandbox.protocol.Fanout] waits for every peer,
    compares signatures, and returns **peer 0's** message — so every other peer's
    payload is deserialized and dropped. Under tensor parallelism those payloads
    are whole activations, identical on every rank because the gather already made
    them whole, so at degree 8 seven eighths of this leg was tensors serialized,
    sent, rebuilt and discarded.

    This sends the signature alone, which is the type of the message. The barrier
    is untouched: still one message per event per peer, in the same order, so a
    peer that resumed a worker while another finished is still caught. What is
    given up is the *option* of ever comparing payloads across ranks — which was
    never taken, since the signature defaults to the type and comparing whole
    tensors at every location would cost more than the interleave it guards.

    Every tensor-parallel shard is a follower by construction; rank 0 is the actor
    and always the leader, which is why this needs no rank plumbing.
    """

    def send(self, value) -> None:
        super().send(signature_of(value))


class Sandbox:
    """A running runner process, reachable over its Unix socket.

    ``process`` is the `Popen` of a runner `spawn` started, or the pid of one a
    `Template` forked, with that ``template``.
    """

    def __init__(self, path: str, process, template: "Optional[Template]" = None):
        self.path = path
        self.process = process
        self.template = template

    def connection(self, timeout: float = 60.0) -> Connection:
        return Connection.open(self.path, timeout)

    def stop(self) -> None:
        """Kill the runner and return without waiting for it to exit.

        Its exit takes ~150 ms, which is the CUDA context being torn down, and the
        actor stops a request's runner before it answers the request. SIGKILL
        rather than SIGTERM, because the block may have installed a handler.
        """
        if self.template is not None:
            self.template.kill(self.process)
        elif self.process.poll() is None:
            self.process.kill()
            threading.Thread(target=self.process.wait, daemon=True).start()
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass


def socket_path() -> str:
    return f"/tmp/sbx-{uuid.uuid4().hex[:12]}.sock"


def spawn(
    python: str = sys.executable,
    quiet: bool = True,
    timeout: float = 30.0,
    peers: int = 1,
    model_key: Optional[str] = None,
    template: bool = False,
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
    behaviour before meta models, and what a standalone runner gets. Whether
    the checkpoint's own code may run during that build is in the key.

    ``template`` launches a `Template` instead, which forks runners rather than
    serving requests itself.
    """
    path = socket_path()
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
    # output is forwarded as `Print` messages (see runner.Writer), so anything reaching
    # this stderr is the runner process failing on its own account -- including
    # `serve`'s "runner: ..." handler, which is the only report of a request the
    # runner could not finish. Sending it to DEVNULL made a runner that died
    # mid-request indistinguishable from one that hung, and cost hours.
    #
    # This process's pid rides along so the runner can tell when it is gone
    # (`runner.die_with_parent`).
    argv = [
        python,
        "-m",
        "ndif.services.ray.sandbox.runner",
        path,
        str(peers),
        str(os.getpid()),
        "template" if template else "runner",
    ]
    if model_key is not None:
        argv.append(model_key)
    process = subprocess.Popen(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL if quiet else None,
        stderr=None,
    )

    try:
        wait_for(path, lambda: process.poll() is not None, timeout)
    except TimeoutError:
        process.kill()
        raise
    return Sandbox(path, process)


def wait_for(path: str, exited, timeout: float) -> None:
    """Wait until a runner accepts connections at ``path``.

    Raises ``RuntimeError`` as soon as ``exited()`` says the process is gone, and
    ``TimeoutError`` after ``timeout`` seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.connect(path)
                return
            except OSError:
                pass
        if exited():
            raise RuntimeError("runner process exited before its socket was ready")
        time.sleep(0.02)
    raise TimeoutError("timed out waiting for the runner socket")


def running(pid: int) -> bool:
    """Whether ``pid`` is alive: neither gone nor a zombie waiting to be reaped."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, IndexError):
        return False


class Template:
    """A runner process that forks the pool's runners (``runner.serve_template``).

    It does the start-up every runner needs once: the imports and the meta
    model, ~4 s. Each runner it forks is then ready in ~30 ms. It is started by
    `spawn`, so it runs with the same allowlisted environment as any runner,
    and its runners inherit nothing more.

    It is the parent of the runners it forks, so it is also what kills them
    (`Sandbox.stop`).
    """

    def __init__(
        self,
        python: str = sys.executable,
        quiet: bool = True,
        peers: int = 1,
        model_key: Optional[str] = None,
        timeout: float = 300.0,
    ):
        self.server = spawn(python, quiet, timeout, peers, model_key, template=True)

    def fork(self, timeout: float = 60.0) -> Sandbox:
        """A new runner, accepting connections."""
        path = socket_path()
        connection = Connection.open(self.server.path)
        try:
            connection.send(f"fork {path}")
            pid = int(connection.recv())
        finally:
            connection.close()
        sandbox = Sandbox(path, pid, self)
        try:
            wait_for(path, lambda: not running(pid), timeout)
        except Exception:
            sandbox.stop()
            raise
        return sandbox

    def kill(self, pid: int) -> None:
        try:
            connection = Connection.open(self.server.path)
        except OSError:
            return  # the template is gone, and its runners go with it
        try:
            connection.send(f"kill {pid}")
        finally:
            connection.close()

    def close(self) -> None:
        self.server.stop()


class Pool:
    """A warm pool that hands out one *fresh* runner per request.

    Each request gets its own runner process and stops it when done, so nothing —
    compiled trace blocks, globals, module state — leaks between requests (the
    isolation the sandbox is for). The pool keeps up to ``size`` runners pre-warmed
    in the background so acquiring one doesn't pay python/nnsight startup; taking
    one triggers a refill. Callers must ``stop()`` what they acquire — there's no
    release-back-to-pool.

    Runners are forked from one `Template`, started by the first refill. If it
    cannot start, every runner is started from nothing instead (`spawn`), as
    before templates existed; if it dies, the next refill starts another.
    """

    def __init__(
        self,
        size: int = 2,
        python: str = sys.executable,
        quiet: bool = True,
        peers: int = 1,
        model_key: Optional[str] = None,
    ):
        self.size = size
        self.python = python
        self.quiet = quiet
        self.peers = peers
        self.model_key = model_key
        self.ready: "queue.Queue" = queue.Queue()
        self.lock = threading.Lock()
        self.spawning = 0
        self.closed = False
        # None until the first refill starts it; False if it could not start.
        self.template: "Template | bool | None" = None
        self.template_lock = threading.Lock()
        for _ in range(size):
            self.refill()

    def _spawn(self) -> Sandbox:
        """One runner, configured the way this pool's runners are."""
        with self.template_lock:
            if self.template is None:
                try:
                    self.template = Template(
                        self.python, self.quiet, self.peers, self.model_key
                    )
                except Exception as error:
                    print(
                        f"sandbox: no runner template ({error!r}); "
                        "starting each runner from nothing",
                        file=sys.stderr,
                        flush=True,
                    )
                    self.template = False
            template = self.template
        if not template:
            return spawn(
                python=self.python,
                quiet=self.quiet,
                peers=self.peers,
                model_key=self.model_key,
            )
        try:
            return template.fork()
        except (ConnectionError, FileNotFoundError):
            # The template died. The next runner starts another.
            with self.template_lock:
                if self.template is template:
                    self.template = None
            template.close()
            raise

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
        with self.template_lock:
            template, self.template = self.template, None
        if template:
            template.close()
