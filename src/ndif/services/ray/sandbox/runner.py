"""The sandbox runner: a separate process that runs the user's traced block.

Binds a Unix socket and serves: each connection delivers the request payload
(``(blob, compress)``); the runner deserializes and executes it with ``nns.run``,
which drives the exchange with events (INTERLEAVE/PRINT/END/EXCEPTION). Importing
``nns`` here — in the runner, never on the host — installs the IPC envoy/interleaver
patches. The process is not hardened (no namespaces, seccomp, rlimits, or
filesystem jail); what it provides is separation from the model actor and a fresh
process per request.

Run standalone:  python -m sandbox.runner /path/to/socket
"""

import contextlib
import os
import socket
import sys
import threading
import time

from . import nns
from .protocol import Channel, Fanout, pack

_PARENT_POLL_S = 1.0


def die_with_parent() -> None:
    """Exit when the model actor that spawned this runner goes away.

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
    """
    parent = os.getppid()

    def watch() -> None:
        while True:
            time.sleep(_PARENT_POLL_S)
            # Reparented (to init, or to whatever subreaper claims us) means the
            # actor is gone.
            if os.getppid() != parent:
                os._exit(1)

    threading.Thread(target=watch, daemon=True, name="die-with-parent").start()


class Connection(Channel):
    """The runner's framed channel to the host (used by ``nns.run`` and ``Writer``).

    ``send`` ships a runner -> host message (event name + values + kwargs); ``recv``
    reads one host -> runner message. See ``protocol.py`` for the message catalog.
    """

    def send(self, event, *values, **kwargs):
        self.send_raw(pack((event, *values), kwargs))


class Writer:
    """Forwards the user's stdout to the host as PRINT events, one per complete
    line (like the in-process ``LogStream``) so a lone ``"\\n"`` from ``print``
    doesn't become an empty LOG. ``flush`` drains a trailing partial line.

    Takes anything that can `send` an event, so the same writer serves one host or
    a whole group of them."""

    def __init__(self, connection):
        self.connection = connection
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.connection.send("PRINT", line)
        return len(text)

    def flush(self):
        if self.buffer:
            self.connection.send("PRINT", self.buffer)
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


class Runner:
    """Serves user code over a Unix socket.

    ``peers`` is how many hosts drive one request. One is the ordinary case: a
    single model actor. Several means the hosts are *ranks of one model* — they run
    the same forward, joined by collectives, so the block must be run once for all
    of them and every reply must reach all of them. `Fanout` is what makes that the
    same code as the single case.
    """

    def __init__(self, path: str, peers: int = 1):
        self.path = path
        self.peers = peers
        if os.path.exists(path):
            os.unlink(path)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(path)
        self.socket.listen()

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
                    channels.append(Connection(sock))
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
        # The host sends the request payload; deserialize and run it here (nns.run
        # reports its own END/EXCEPTION). All stdout *and stderr* from the traced
        # block is forwarded to the host as PRINT events — stderr because that is
        # where `warnings.warn` goes, and a warning only the runner sees is a
        # warning nobody reads. Separate writers so their partial lines don't
        # interleave.
        blob, compress, dtype, *rest = leader.recv()
        seed = rest[0] if rest else None
        with contextlib.redirect_stdout(Writer(connection)):
            with contextlib.redirect_stderr(Writer(connection)):
                nns.run(connection, blob, compress, dtype, seed)


if __name__ == "__main__":
    die_with_parent()
    peers = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    Runner(sys.argv[1], peers=peers).serve()
