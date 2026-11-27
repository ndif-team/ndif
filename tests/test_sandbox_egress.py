"""A confined runner's network (``NDIF_SANDBOX_EGRESS``).

With an egress proxy named, a runner starts in a network namespace of its own
and reaches the proxy through a Unix socket (``egress.py``). The proxy here is a
few lines of this file: it answers every plain HTTP request and records it,
which is enough to tell what reached it and what did not. The real proxy is
not run here; its policy has ``test_egress_policy.py``.

The tests that confine a runner use a real gpt2 on CPU, as
``test_sandbox_template.py`` does, and skip on a host that does not let the user
create a user namespace (docker's default seccomp profile is one).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time

import pytest

torch = pytest.importorskip("torch")
nnsight = pytest.importorskip("nnsight")

from test_sandbox_iter_pins import (  # noqa: F401  (model is a fixture)
    PROMPT,
    Capture,
    model,
)
from test_sandbox_template import run

from ndif.services.ray.sandbox import egress, host

def script(code: str) -> subprocess.CompletedProcess:
    """Run ``code`` in a new process, the way `host.spawn` runs ``egress.py``:
    isolated, and before anything has started a thread."""
    return subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True
    )


needs_namespaces = pytest.mark.skipif(
    # Not `leave_network`: broken, it would skip the tests that say so.
    script("import os; os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET)").returncode
    != 0,
    reason="this host does not allow unprivileged user namespaces",
)


class Proxy:
    """Stands in for the egress proxy: answers any plain HTTP request it is
    sent, and hands out an authority the way mitmproxy does."""

    def __init__(self, port: int = 0) -> None:
        self.authority = certificate()
        self.seen: "list[str]" = []
        self.listener = socket.create_server(("127.0.0.1", port))
        self.port = self.listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            with connection:
                head = b""
                while b"\r\n\r\n" not in head and (data := connection.recv(65536)):
                    head += data
                if not head:
                    continue
                line = head.split(b"\r\n", 1)[0].decode()
                self.seen.append(line)
                body = self.authority if "mitm.it" in line else b"through the proxy"
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
                    b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
                )

    def close(self) -> None:
        self.listener.close()


def certificate() -> bytes:
    """A real certificate to hand out as the proxy's: a runner's HTTP libraries
    load the whole bundle, and refuse one that holds anything else."""
    import certifi

    with open(certifi.where(), "rb") as file:
        first = file.read().split(b"-----BEGIN CERTIFICATE-----")[1]
    return b"-----BEGIN CERTIFICATE-----" + first.rstrip() + b"\n"


@pytest.fixture
def proxy():
    proxy = Proxy()
    yield proxy
    proxy.close()


def within(seconds: float, condition) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_a_relay_passes_both_directions_and_ends_with_either_side():
    target = socket.create_server(("127.0.0.1", 0))
    listener = socket.create_server(("127.0.0.1", 0))
    egress.relay(listener, lambda: socket.create_connection(target.getsockname()))
    try:
        near = socket.create_connection(listener.getsockname(), timeout=5)
        far, _ = target.accept()
        far.settimeout(5)
        near.sendall(b"out")
        assert far.recv(3) == b"out"
        far.sendall(b"back")
        assert near.recv(4) == b"back"
        far.close()
        assert near.recv(1) == b""
        near.close()
    finally:
        listener.shutdown(socket.SHUT_RDWR)
        listener.close()
        target.close()


def test_a_door_adds_the_proxys_authority_once_the_proxy_answers(monkeypatch):
    import certifi

    monkeypatch.setattr(egress.Door, "RETRY", 0.05)
    # The proxy is not up yet: a port nothing listens on.
    with socket.create_server(("127.0.0.1", 0)) as free:
        port = free.getsockname()[1]
    door = egress.Door(f"http://127.0.0.1:{port}")
    try:
        with open(certifi.where(), "rb") as file:
            usual = file.read()
        time.sleep(0.3)
        with open(door.bundle, "rb") as file:
            assert file.read() == usual

        proxy = Proxy(port)
        try:
            assert within(
                5,
                lambda: open(door.bundle, "rb").read()
                == usual + b"\n" + proxy.authority,
            )
        finally:
            proxy.close()
    finally:
        door.close()
    assert not os.path.exists(door.path)
    assert not os.path.exists(door.bundle)


def test_a_setting_without_a_host_and_a_port_is_refused():
    with pytest.raises(ValueError, match=egress.PROXY):
        egress.Door("egress:3128")


def test_a_runner_that_cannot_be_confined_does_not_start():
    path = host.socket_path()
    refused = script(
        "import os, runpy, sys\n"
        "def refuse(flags): raise PermissionError(1, 'Operation not permitted')\n"
        "os.unshare = refuse\n"
        f"sys.argv = ['egress.py', sys.executable, '-m', 'runner', {path!r}]\n"
        f"runpy.run_path({egress.__file__!r}, run_name='__main__')\n"
    )
    assert refused.returncode == 1
    assert "could not leave the network" in refused.stderr
    assert not os.path.exists(path)


def fetch_and_dial(model, port: int) -> bytes:
    """A request whose block fetches a URL, reads the certificates it was told
    to trust, tries the proxy's own address without the door, and names its
    tokenizer, which it has only if the runner built its meta model."""
    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        import os
        import socket
        import urllib.request

        hidden = model.transformer.h[5].output.save()
        fetched = nnsight.save(
            urllib.request.urlopen("http://files.test/weights", timeout=10).read()
        )
        with open(os.environ["SSL_CERT_FILE"], "rb") as file:
            trusted = nnsight.save(file.read())
        tokenizer = nnsight.save(type(model.tokenizer).__name__)
        try:
            socket.create_connection(("127.0.0.1", port), timeout=5).close()
            direct = nnsight.save("connected")
        except OSError as error:
            direct = nnsight.save(type(error).__name__)
    return capture.blob


@needs_namespaces
def test_a_confined_block_reaches_the_proxy_and_nothing_else(model, proxy, monkeypatch):
    with model.trace(PROMPT):
        hidden = model.transformer.h[5].output.save()
    local = hidden

    monkeypatch.setenv(egress.PROXY, proxy.url)
    pool = host.Pool(size=1, model_key=model.to_model_key())
    try:
        sandbox = pool.acquire(timeout=120)
        try:
            saved = run(model, sandbox, fetch_and_dial(model, proxy.port))
        finally:
            sandbox.stop()
        # Forked from the template, as a pool's runners are.
        assert sandbox.template is not None
    finally:
        pool.close()

    assert saved["fetched"] == b"through the proxy"
    assert "GET http://files.test/weights HTTP/1.1" in proxy.seen
    assert saved["trusted"].endswith(proxy.authority)
    # The runner built its meta model, and from the cache: its start-up asked
    # the proxy for nothing.
    assert saved["tokenizer"] != "NoneType"
    assert not [line for line in proxy.seen if "huggingface" in line]
    # The proxy listens on this host's loopback. The runner's loopback is its own.
    assert saved["direct"] == "ConnectionRefusedError"
    # The model's forward is the host's, as ever.
    assert torch.allclose(saved["hidden"], local)


@needs_namespaces
def test_a_pool_without_a_template_confines_its_runners_too(model, proxy, monkeypatch):
    def no_template(*args, **kwargs):
        raise RuntimeError("no template")

    monkeypatch.setattr(host, "Template", no_template)
    monkeypatch.setenv(egress.PROXY, proxy.url)
    pool = host.Pool(size=1, model_key=model.to_model_key())
    try:
        sandbox = pool.acquire(timeout=120)
        try:
            saved = run(model, sandbox, fetch_and_dial(model, proxy.port))
        finally:
            sandbox.stop()
        # Started from nothing, which is what a pool does without a template.
        assert sandbox.template is None
    finally:
        pool.close()
    assert saved["fetched"] == b"through the proxy"
    assert saved["direct"] == "ConnectionRefusedError"
    assert saved["tokenizer"] != "NoneType"


@needs_namespaces
def test_a_confined_runner_starts_while_its_proxy_is_down(model):
    with socket.create_server(("127.0.0.1", 0)) as free:
        port = free.getsockname()[1]
    door = egress.Door(f"http://127.0.0.1:{port}")
    capture = Capture()
    with model.trace(PROMPT, backend=capture):
        import urllib.request

        model.transformer.h[5].output
        tokenizer = nnsight.save(type(model.tokenizer).__name__)
        try:
            urllib.request.urlopen("http://files.test/weights", timeout=10)
            fetched = nnsight.save("fetched")
        except OSError as error:
            fetched = nnsight.save(type(error).__name__)
    try:
        started = time.time()
        sandbox = host.spawn(model_key=model.to_model_key(), timeout=120, door=door)
        elapsed = time.time() - started
        try:
            saved = run(model, sandbox, capture.blob)
        finally:
            sandbox.stop()
    finally:
        door.close()
    assert saved["tokenizer"] != "NoneType"
    # What the troubleshooting page tells an operator to look for.
    assert saved["fetched"] == "RemoteDisconnected"
    # With the Hub asked and unreachable, huggingface_hub retries for ~45 s.
    assert elapsed < 30


def test_without_the_setting_a_pool_opens_no_door(monkeypatch):
    monkeypatch.delenv(egress.PROXY, raising=False)
    pool = host.Pool(size=0)
    try:
        assert pool.door is None
    finally:
        pool.close()
