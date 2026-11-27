"""The egress proxy's policy (``docker/egress/allowlist.py``), one request at a
time.

The policy is a mitmproxy addon, and NDIF itself does not need mitmproxy, so
this skips where it is not installed. No proxy is started: each case builds the
request mitmproxy would hand the addon and reads what the addon did to it.
"""

import importlib.util
import pathlib

import pytest

pytest.importorskip("mitmproxy")

from mitmproxy.test import tflow

POLICY = pathlib.Path(__file__).parents[1] / "docker" / "egress" / "allowlist.py"


@pytest.fixture
def policy(monkeypatch):
    spec = importlib.util.spec_from_file_location("allowlist", POLICY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ALLOW", ["open.test"])
    monkeypatch.setattr(module, "READ_ONLY", ["files.test"])
    return module


def request(method: str, host: str, name: "str | None" = None, port: int = 443):
    """A request the proxy would send to ``host``:``port``, whose Host header
    says ``name``."""
    flow = tflow.tflow()
    flow.request.method = method
    flow.request.host = host
    flow.request.port = port
    flow.request.headers["Host"] = name or host
    return flow


@pytest.mark.parametrize(
    "method, host, name, port, passes",
    [
        ("GET", "files.test", None, 443, True),
        ("HEAD", "sub.files.test", None, 443, True),
        ("POST", "files.test", None, 443, False),
        ("PUT", "files.test", None, 80, False),
        ("POST", "open.test", None, 443, True),
        ("GET", "other.test", None, 443, False),
        ("GET", "notfiles.test", None, 443, False),
        ("GET", "files.test", None, 8080, False),
        # The Host header is the client's to write. The proxy connects to `host`.
        ("GET", "other.test", "files.test", 80, False),
        ("POST", "other.test", "open.test", 80, False),
        ("GET", "files.test", "other.test", 443, False),
        ("POST", "files.test", "open.test", 443, False),
        ("GET", "mitm.it", None, 80, True),
        ("GET", "other.test", "mitm.it", 80, False),
    ],
)
def test_a_request_is_decided_on_where_it_goes_and_the_name_it_asks_for(
    policy, method, host, name, port, passes
):
    flow = request(method, host, name, port)
    policy.requestheaders(flow)
    if passes:
        assert flow.response is None
    else:
        assert flow.response.status_code == 403


def test_a_refused_request_whose_body_is_streamed_is_dropped(policy):
    flow = request("POST", "files.test")
    flow.request.stream = True
    policy.requestheaders(flow)
    assert flow.response is None
    assert flow.error is not None


@pytest.mark.parametrize(
    "host, port, passes",
    [
        ("files.test", 443, True),
        ("open.test", 443, True),
        ("other.test", 443, False),
        ("files.test", 8443, False),
    ],
)
def test_a_tunnel_is_opened_only_to_a_listed_host_on_a_web_port(policy, host, port, passes):
    flow = request("CONNECT", host, port=port)
    policy.http_connect(flow)
    assert (flow.response is None) == passes
