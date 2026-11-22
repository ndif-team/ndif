"""The sandbox runner: a separate process that runs the user's traced block.

Binds a Unix socket and serves: each connection delivers the request (a
``Payload``); the runner deserializes and executes it with ``execute.run``, which
drives the exchange with the messages in ``messages.py``. A `Runner` calls
``adapters.install`` when it is built — in the runner, never on the host — which
replaces the nnsight methods that assume the model is in this process. The
process is not hardened (no namespaces, seccomp, rlimits, or filesystem jail);
what it provides is separation from the model actor and a fresh process per
request.

A pool does not start each runner from nothing: it starts one *template*, which
builds what every runner needs and forks a runner per request
(`serve_template`).

Run standalone:
    python -m ndif.services.ray.sandbox.runner <socket> <peers> <parent pid> runner|template [model_key]
"""

import contextlib
import os
import signal
import socket
import sys
import threading
import time
import traceback

from . import adapters, execute, meta
from .messages import Print
from .protocol import Channel, Fanout

_PARENT_POLL_S = 1.0


def die_with_parent(parent: int) -> None:
    """Exit when ``parent``, the process that started this one, goes away.

    Without this a runner outlives its actor. The graceful paths already stop it
    (``cleanup`` -> ``discard_sandbox`` after every request, ``interrupt`` on a
    timeout or cancel), but eviction is ``ray.kill(no_restart=True)`` and an
    unrecoverable-CUDA restart is ``ray.kill(no_restart=False)`` — both SIGKILL
    the actor, so no Python cleanup of any kind runs. A runner executing a
    runaway block at that moment is orphaned and keeps spinning at 100% of a
    core until the container restarts, invisible to ``ndif status`` because its
    deployment is gone. Re-queueing leaks one of these per eviction.

    Polling ``getppid`` rather than ``prctl(PR_SET_PDEATHSIG)``: that signal
    fires when the parent **thread** exits, not the parent process, and the pool
    warms every runner on a short-lived ``threading.Thread`` (``host.Pool.refill``)
    while ``acquire`` spawns from the per-request execution thread. Armed with
    prctl, every runner is killed seconds after it is created and the whole
    sandbox path fails with connection-refused.

    A daemon thread costs nothing and is thread-agnostic. A pure-Python runaway
    block still yields the GIL on the interpreter's switch interval, so the
    watchdog keeps getting scheduled while user code spins.

    ``parent`` is the pid the parent passed in, not ``getppid()`` read here. This
    runs after the imports, over a second into the process, and a parent that
    died in that second has already been replaced by init: read here, init
    became "the parent" and was watched forever. A runner the pool started just
    before its actor died then idled in ``accept`` for good.
    """

    def watch() -> None:
        # Reparented (to init, or to whatever subreaper claims us) means the
        # parent is gone.
        while os.getppid() == parent:
            time.sleep(_PARENT_POLL_S)
        os._exit(1)

    threading.Thread(target=watch, daemon=True, name="die-with-parent").start()


class Writer:
    """Forwards the user's stdout to the host as `Print` messages, one per complete
    line (like the in-process ``LogStream``) so a lone ``"\\n"`` from ``print``
    doesn't become an empty LOG. ``flush`` drains a trailing partial line.

    Takes anything that can `send` a message, so the same writer serves one host or
    a whole group of them."""

    def __init__(self, connection):
        self.connection = connection
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.connection.send(Print(line))
        return len(text)

    def flush(self):
        if self.buffer:
            self.connection.send(Print(self.buffer))
            self.buffer = ""


def _hung_up(sock) -> bool:
    """Whether the peer has already closed, without consuming anything it sent.

    A closed peer reads as end-of-stream; one that is connected but has not
    spoken yet has nothing to read, which is the ordinary state of a host waiting
    for the group to assemble.
    """
    try:
        return sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
    except BlockingIOError:
        return False
    except OSError:
        return True


def build(model_key: "str | None") -> None:
    """What a runner does before it serves, which a forked runner inherits.

    Replaces the nnsight methods that assume the model is in this process, then
    builds the meta model for ``model_key`` (see `Runner` for why a failed build
    is only reported).
    """
    # Before anything else: from here on, a model call in this process goes
    # to the host over the socket.
    adapters.install()
    if model_key is not None:
        try:
            meta.load_meta_model(model_key)
        except Exception as error:
            print(
                f"runner: no meta model for {model_key!r} ({error!r}); "
                "module and tokenizer ids will resolve to None",
                file=sys.stderr,
                flush=True,
            )


def listen(path: str) -> socket.socket:
    if os.path.exists(path):
        os.unlink(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    sock.listen()
    return sock


class Runner:
    """Serves user code over a Unix socket.

    ``peers`` is how many hosts drive one request. One is the ordinary case: a
    single model actor. Several means the hosts are *ranks of one model* — they run
    the same forward, joined by collectives, so the block must be run once for all
    of them and every reply must reach all of them. `Fanout` is what makes that the
    same code as the single case.

    ``model_key`` is the checkpoint whose *meta* model this process builds, so a
    request's module / tokenizer ids resolve to real weightless objects here. It is
    built **before the socket binds**, so a host that can connect is a host whose
    runner is genuinely ready — the pool's readiness wait covers the build. Without
    one those ids resolve to ``None``, which is what a standalone runner gets.

    A build that fails is reported and then ignored, on purpose. The map is an
    optimization: with none, every id resolves to ``None`` and the runner serves
    requests exactly as it did before meta models existed. Raising here instead
    would kill the process before it binds, which `Pool.refill` swallows on its
    warm thread — so one unbuildable checkpoint would present as every request
    waiting out `acquire`'s 30s timeout with nothing in the actor's log.
    """

    def __init__(
        self,
        path: str,
        peers: int = 1,
        model_key: "str | None" = None,
    ):
        self.path = path
        self.peers = peers
        build(model_key)
        self.socket = listen(path)

    def serve(self):
        while True:
            channels = []
            try:
                while len(channels) < self.peers:
                    sock, _ = self.socket.accept()
                    if _hung_up(sock):
                        # `spawn` probes the socket to learn this process is up,
                        # by connecting and closing again. With one peer that
                        # probe merely caused a failed exchange and a retry; with
                        # several it would take one of the group's places and the
                        # runner would wait forever for a host that already left.
                        sock.close()
                        continue
                    channels.append(Channel(sock))
                self.handle(channels)
            except Exception as error:
                # A dropped probe or a failing exchange must not kill the loop.
                print(f"runner: {error}", file=sys.stderr, flush=True)
            finally:
                for channel in channels:
                    channel.close()

    def handle(self, channels):
        # The payload comes from the first host only -- every host would send the
        # same bytes, and the block is deserialized once here regardless.
        #
        # "First" is by connection order, so the host that sends the payload has to
        # be the one that connects first. Under tensor parallelism rank 0 opens its
        # connection before it tells the shards where the runner is, which orders
        # them by construction.
        leader = channels[0]
        connection = Fanout(channels) if len(channels) > 1 else leader
        # The host sends the request (a `Payload`); deserialize and run it here
        # (execute.run reports its own End or Failed). All stdout *and stderr* from the
        # traced block is forwarded to the host as `Print` messages — stderr because
        # that is where `warnings.warn` goes, and a warning only the runner sees is
        # a warning nobody reads. Separate writers so their partial lines don't
        # interleave.
        payload = leader.recv()
        with contextlib.redirect_stdout(Writer(connection)):
            with contextlib.redirect_stderr(Writer(connection)):
                execute.run(
                    connection,
                    payload.blob,
                    payload.compress,
                    payload.dtype,
                    payload.seed,
                    payload.env,
                )


def serve_template(path: str, peers: int, model_key: "str | None") -> None:
    """Build once, then fork a runner for each request.

    A runner started from nothing takes ~4 s, nearly all of it imports and the
    meta model. A fork of a process that has done both is ready in ~30 ms. So a pool
    keeps one of these (``host.Template``), and it answers two commands on its
    socket:

    * ``fork <path>``: fork a runner that serves at ``<path>``. The reply is its pid.
    * ``kill <pid>``: SIGKILL a runner this process forked. No reply.

    Exited runners are reaped here, and only between commands, so a pid this is
    told to kill is still this process's child: never a pid the system has
    handed to something else.

    A child forked after its parent opened the GPU driver cannot use CUDA. A
    model's own code can open it while the meta model builds, so a template
    that finds it open exits, and the pool starts runners from nothing instead.
    """
    build(model_key)
    if cuda_started():
        sys.exit("runner template: building the meta model started CUDA; not forking")
    listener = listen(path)
    template = os.getpid()
    children: "set[int]" = set()
    while True:
        sock, _ = listener.accept()
        try:
            reap(children)
            channel = Channel(sock)
            command, _, argument = channel.recv().partition(" ")
            if command == "fork":
                pid = os.fork()
                if pid == 0:
                    listener.close()
                    sock.close()
                    serve_forked(argument, peers, template)
                children.add(pid)
                channel.send(str(pid))
            elif command == "kill" and int(argument) in children:
                os.kill(int(argument), signal.SIGKILL)
        except ConnectionError:
            pass  # `spawn`'s readiness probe connects and says nothing
        except Exception as error:
            print(f"runner template: {error!r}", file=sys.stderr, flush=True)
        finally:
            sock.close()


def serve_forked(path: str, peers: int, template: int) -> None:
    """The child side of a fork: a runner of its own, which never returns."""
    try:
        die_with_parent(template)
        reseed()
        Runner(path, peers).serve()
    except BaseException:
        traceback.print_exc()
    finally:
        os._exit(1)


def reseed() -> None:
    """Give a forked runner its own random state.

    A fork starts with a copy of its template's, so without this every runner
    would draw the same numbers. A runner started from nothing draws its own.
    """
    import random

    import torch

    random.seed()
    torch.seed()
    numpy = sys.modules.get("numpy")
    if numpy is not None:
        numpy.random.seed()


def reap(children: "set[int]") -> None:
    for pid in list(children):
        if os.waitpid(pid, os.WNOHANG)[0]:
            children.discard(pid)


def cuda_started() -> bool:
    """Whether this process has the GPU driver open."""
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}").startswith("/dev/nvidia"):
                return True
        except OSError:
            pass
    return False


if __name__ == "__main__":
    # <socket> <peers> <parent pid> runner|template [model_key]
    path, peers, parent, mode = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    model_key = sys.argv[5] if len(sys.argv) > 5 else None
    die_with_parent(parent)
    if mode == "template":
        serve_template(path, peers, model_key)
    else:
        Runner(path, peers=peers, model_key=model_key).serve()
