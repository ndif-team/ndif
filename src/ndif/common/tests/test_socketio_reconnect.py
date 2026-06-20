"""Concurrent SioProvider reconnects must be serialized.

When several Dispatcher coroutines deliver a status update while the socket.io
connection is down, they all reach the reconnect path. ``async_connect`` owns
the connection lifecycle (reset + rebuild + connect) under its lock, so one
task rebuilds the connection while the others wait and reuse it — every emit
should complete cleanly.

This test drives that shape directly: N pairs of concurrent ``async_emit``
against a local socket.io server while disconnected, asserting every emit
succeeds with no exception. Self-contained — spins up its own socket.io
server, no live NDIF stack needed.
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from ndif.common.providers.socketio import SioProvider

pytest.importorskip("aiohttp")
import socketio  # noqa: E402  (hard dep of the module under test)
from aiohttp import web  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _run_concurrent_emits(iterations: int = 10, concurrency: int = 2) -> list:
    port = _free_port()
    server = socketio.AsyncServer(async_mode="aiohttp")

    @server.event
    async def connect(sid, environ):
        # Widen the handshake window so a concurrent reconnect deterministically
        # interleaves with an in-flight connect.
        await asyncio.sleep(0.05)

    @server.event
    async def blocking_response(sid, *args):
        return "ok"

    app = web.Application()
    server.attach(app, socketio_path="/ws/socket.io")
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()

    # Point the provider at the local server. max_retries=1 so a reconnect
    # failure surfaces instead of being retried into a pass.
    SioProvider.api_url = f"http://127.0.0.1:{port}"
    SioProvider.max_retries = 1
    SioProvider.retry_interval = 0.01

    results: list = []
    try:
        for _ in range(iterations):
            # Fresh, disconnected state — forces every emit through the reconnect path.
            SioProvider._async_sio = None
            SioProvider._async_connect_lock = None
            res = await asyncio.gather(
                *(
                    SioProvider.async_emit("blocking_response", data=("sid", b"x"))
                    for _ in range(concurrency)
                ),
                return_exceptions=True,
            )
            results.extend(res)
            try:
                await SioProvider.async_reset()
            except Exception:
                pass
    finally:
        try:
            await SioProvider.async_reset()
        except Exception:
            pass
        await runner.cleanup()
    return results


def test_concurrent_async_emit_does_not_race_on_reconnect():
    results = asyncio.run(_run_concurrent_emits())
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, (
        f"{len(failures)}/{len(results)} concurrent emits raised "
        f"{sorted({type(e).__name__ for e in failures})} — reconnect is not serialized"
    )


if __name__ == "__main__":  # allow standalone run without pytest
    test_concurrent_async_emit_does_not_race_on_reconnect()
    print("PASS")
