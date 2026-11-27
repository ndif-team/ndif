"""A confined runner's network: none of its own, and one way out.

Where the deployment names an egress proxy (``NDIF_SANDBOX_EGRESS``),
`host.spawn` starts a runner through this file:

    python -I egress.py <the runner's command>

The process moves into a network namespace of its own, which holds a loopback
interface and nothing else (`leave_network`), opens the one way out of it
(`tunnel`), and then replaces itself with that command, so the pid and the
parent are the ones `host.spawn` started. A pool's template starts this way
too, and the runners it forks are in its namespace.

The way out:

    block --TCP, 127.0.0.1--> tunnel --Unix socket--> the actor's `Door`
        --TCP--> egress proxy --> the hosts its policy allows

The Unix socket is a file, and a network namespace does not separate files, so
it is the one thing both sides can reach. Nothing here decides what a block may
reach. The proxy does, from two lists of host names
(`docker/egress/allowlist.py`): hosts passed through untouched, and hosts that
may only be read from. A block cannot go around it, because nothing else is
reachable. The runner's own start-up does not use it: the meta model is built
from the shared cache alone (`meta.load_meta_model`), so a runner starts the
same whether or not the proxy is up.

A process may create a network namespace only where it holds the privilege to,
so it first creates a user namespace, in which it holds every privilege over
what that namespace owns and none over the host. Its uid and gid are mapped to
themselves, so they read as before. An ordinary user reaches files, caches and
the GPU's device nodes as before, and holds no privilege once the process
becomes the runner. Root, which the docker image runs as, differs on both
counts: it keeps its privileges inside the namespace, and it loses root's pass
on files that other users own, so its runner reads a cache directory only if
root owns it or its mode lets anyone.

A process can create a user namespace only while it has one thread, and
importing torch starts more. So this file is run by path, and imports the
standard library only: run with ``-m`` it would import this package first, and
the package imports torch. ``-I`` keeps this directory off the front of
``sys.path``, where a file here could shadow a standard module.

A runner that cannot be confined does not start.
"""

import contextlib
import fcntl
import os
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid

#: The deployment's setting: the egress proxy's address, as a URL
#: (``http://egress:3128``). Unset, runners keep the host's network.
PROXY = "NDIF_SANDBOX_EGRESS"

#: Set on a confined runner by `Door.env`: the path of the actor's door.
DOOR = "NDIF_SANDBOX_EGRESS_DOOR"

#: The tunnel's port on a confined runner's loopback.
PORT = 3128


def leave_network() -> None:
    """Move this process into a network namespace that holds only loopback.

    Raises:
        OSError: this host does not let the user create a user namespace.
    """
    uid, gid = os.getuid(), os.getgid()
    os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)
    # Unmapped, the uid and gid read as `nobody`'s. A user may map its own gid
    # only after giving up `setgroups`.
    for name, value in (
        ("setgroups", "deny"),
        ("uid_map", f"{uid} {uid} 1"),
        ("gid_map", f"{gid} {gid} 1"),
    ):
        with open(f"/proc/self/{name}", "w") as file:
            file.write(value)
    # A new namespace's loopback is down. SIOCSIFFLAGS with IFF_UP brings it up.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        fcntl.ioctl(control, 0x8914, struct.pack("16sH", b"lo", 0x1))


def relay(listener: socket.socket, dial) -> None:
    """Pass each connection accepted on ``listener`` to a new one from ``dial()``,
    both ways, until ``listener`` is shut down."""

    def pump(source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := source.recv(65536):
                sink.sendall(data)
        except OSError:
            pass
        # Either direction ending ends both: the other pump is woken from its
        # `recv` and closes its own end.
        for end in (source, sink):
            with contextlib.suppress(OSError):
                end.shutdown(socket.SHUT_RDWR)
        source.close()

    def serve() -> None:
        while True:
            try:
                near, _ = listener.accept()
            except OSError:
                return
            try:
                far = dial()
            except OSError:
                near.close()
                continue
            for source, sink in ((near, far), (far, near)):
                threading.Thread(target=pump, args=(source, sink), daemon=True).start()

    threading.Thread(target=serve, daemon=True, name="egress-relay").start()


def tunnel(door: str) -> None:
    """Open the way out of this process's namespace: a port on its loopback whose
    connections go to ``door``.

    The relay runs in a child of this process, and so in the same namespace. It
    is not a thread, because a pool's template has to stay free of them to fork.
    It ends when this process does. The port is listening before this returns.
    """

    def dial() -> socket.socket:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(door)
        return connection

    listener = socket.create_server(("127.0.0.1", PORT))
    parent = os.getpid()
    if os.fork():
        listener.close()
        return
    try:
        relay(listener, dial)
        while os.getppid() == parent:
            time.sleep(1.0)
    finally:
        os._exit(0)


class Door:
    """The actor's end: a Unix socket whose connections go to the egress proxy,
    and the certificates its runners trust.

    The proxy decrypts a connection to a read-only host, to see each request's
    method. A runner's HTTP libraries therefore have to trust the proxy's
    certificate authority, which is fetched from the proxy itself and written
    after the usual ones to ``bundle``. A thread asks until the proxy answers,
    because the proxy can start after the actor.
    """

    #: Seconds between two tries while the proxy does not answer.
    RETRY = 3.0

    def __init__(self, proxy: str) -> None:
        import certifi

        url = urllib.parse.urlsplit(proxy)
        if not (url.hostname and url.port):
            raise ValueError(
                f"{PROXY} has to be a URL with a host and a port, like "
                f"http://egress:3128, not {proxy!r}"
            )
        self.proxy = (url.hostname, url.port)
        name = f"/tmp/sbx-egress-{uuid.uuid4().hex[:12]}"
        self.path, self.bundle = name + ".sock", name + ".pem"
        with open(certifi.where(), "rb") as file:
            self.usual = file.read()
        # There from the start, so a runner's HTTP library never finds the file
        # it was told to trust missing.
        with open(self.bundle, "wb") as file:
            file.write(self.usual)
        self.closed = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(self.path)
        self.listener.listen()
        relay(self.listener, self.dial)
        threading.Thread(target=self.add_authority, daemon=True, name="egress-bundle").start()
        print(
            f"sandbox: runners are confined to the egress proxy at "
            f"{url.hostname}:{url.port}",
            file=sys.stderr,
            flush=True,
        )

    def dial(self) -> socket.socket:
        connection = socket.create_connection(self.proxy, timeout=10)
        connection.settimeout(None)
        return connection

    def env(self) -> dict:
        """What a confined runner's environment adds."""
        tunnel = f"http://127.0.0.1:{PORT}"
        return {
            DOOR: self.path,
            "HTTP_PROXY": tunnel,
            "HTTPS_PROXY": tunnel,
            "http_proxy": tunnel,
            "https_proxy": tunnel,
            "SSL_CERT_FILE": self.bundle,
            "REQUESTS_CA_BUNDLE": self.bundle,
            "CURL_CA_BUNDLE": self.bundle,
            # Hugging Face's xet client does its own networking and ignores
            # the proxy settings.
            "HF_HUB_DISABLE_XET": "1",
        }

    def add_authority(self) -> None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": "http://%s:%s" % self.proxy})
        )
        warned = False
        while not self.closed.is_set():
            try:
                # Where mitmproxy hands out its authority, to anything behind it.
                authority = opener.open("http://mitm.it/cert/pem", timeout=15).read()
            except OSError as error:
                if not warned:
                    warned = True
                    print(
                        f"sandbox: no certificate from the egress proxy at "
                        f"{self.proxy[0]}:{self.proxy[1]} ({error!r}); a block's "
                        "HTTPS requests fail until it answers",
                        file=sys.stderr,
                        flush=True,
                    )
                self.closed.wait(self.RETRY)
                continue
            if not self.closed.is_set():
                # Replaced whole, so a runner never reads half a file.
                with open(self.bundle + ".new", "wb") as file:
                    file.write(self.usual + b"\n" + authority)
                os.replace(self.bundle + ".new", self.bundle)
            return

    def close(self) -> None:
        self.closed.set()
        # Shut down, not only closed: that is what wakes the relay's `accept`.
        with contextlib.suppress(OSError):
            self.listener.shutdown(socket.SHUT_RDWR)
        self.listener.close()
        for path in (self.path, self.bundle):
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)


if __name__ == "__main__":
    try:
        leave_network()
        tunnel(os.environ[DOOR])
    except OSError as error:
        sys.exit(
            f"runner: could not leave the network ({error!r}); not starting. "
            f"{PROXY} needs a host that allows unprivileged user namespaces."
        )
    os.execv(sys.argv[1], sys.argv[1:])
