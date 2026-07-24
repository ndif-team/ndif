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

from . import nns
from .protocol import decode, pack, recv_frame, send_frame


class Connection:
    """The runner's framed channel to the host (used by ``nns.run`` and ``Writer``).

    ``send`` ships a runner -> host message (event name + values + kwargs); ``recv``
    reads one host -> runner message. See ``protocol.py`` for the message catalog.
    """

    def __init__(self, sock):
        self.sock = sock

    def send(self, event, *values, **kwargs):
        send_frame(self.sock, pack((event, *values), kwargs))

    def recv(self):
        return decode(recv_frame(self.sock))

    def print_event(self, text):
        """Forward stdout text to the host as a PRINT event."""
        self.send("PRINT", text)


class Writer:
    """Forwards the user's stdout to the host as PRINT events, one per complete
    line (like the in-process ``LogStream``) so a lone ``"\\n"`` from ``print``
    doesn't become an empty LOG. ``flush`` drains a trailing partial line."""

    def __init__(self, connection):
        self.connection = connection
        self.buffer = ""

    def write(self, text):
        self.buffer += text
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self.connection.print_event(line)
        return len(text)

    def flush(self):
        if self.buffer:
            self.connection.print_event(self.buffer)
            self.buffer = ""


class Runner:
    """Serves user code over a Unix socket."""

    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            os.unlink(path)
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(path)
        self.socket.listen()

    def serve(self):
        while True:
            sock, _ = self.socket.accept()
            try:
                self.handle(sock)
            except Exception as error:
                # A dropped probe or a failing exchange must not kill the loop.
                print(f"runner: {error}", file=sys.stderr, flush=True)
            finally:
                sock.close()

    def handle(self, sock):
        connection = Connection(sock)
        # The host sends the request payload; deserialize and run it here (nns.run
        # reports its own END/EXCEPTION). All stdout from the traced block is
        # forwarded to the host as PRINT events.
        blob, compress = connection.recv()
        with contextlib.redirect_stdout(Writer(connection)):
            nns.run(connection, blob, compress)


if __name__ == "__main__":
    Runner(sys.argv[1]).serve()
