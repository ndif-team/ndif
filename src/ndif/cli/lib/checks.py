"""Lightweight, report-only reachability checks for ``info`` and ``doctor``.

Each returns a bool and never raises, installs, or otherwise touches the
system — a service being down is a normal answer, not an error.
"""

from __future__ import annotations

import socket
from urllib.parse import urlparse


def _tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_redis(url: str) -> bool:
    """True if a Redis server answers PING at ``url``."""
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=1)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


def check_minio(url: str) -> bool:
    """True if something is listening on the object store's host:port."""
    p = urlparse(url)
    return _tcp_open(p.hostname or "localhost", p.port or 9000)


def check_api(url: str) -> bool:
    """True if the API answers its ``/ping`` liveness probe (else TCP-reachable)."""
    try:
        import requests

        return requests.get(f"{url.rstrip('/')}/ping", timeout=2).status_code == 200
    except Exception:
        p = urlparse(url)
        return _tcp_open(p.hostname or "localhost", p.port or 8001)


def check_ray(address: str) -> bool:
    """True if the Ray client server's host:port accepts a connection."""
    host = (address or "").replace("ray://", "")
    if ":" not in host:
        return False
    h, _, port = host.partition(":")
    try:
        return _tcp_open(h or "localhost", int(port))
    except ValueError:
        return False
