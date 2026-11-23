"""A Redis read on a connection that died without closing has to give up.

``RedisProvider.async_socket_timeout`` is the limit; the dispatcher sets it.

No Redis: a local server accepts the connection and never answers.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from ndif.common.providers.redis import RedisProvider


@pytest.fixture
def provider(monkeypatch):
    """RedisProvider, restored to its own clients after the test."""
    monkeypatch.setattr(RedisProvider, "url", RedisProvider.url)
    monkeypatch.setattr(RedisProvider, "async_socket_timeout", None)
    yield RedisProvider
    monkeypatch.undo()
    RedisProvider.connect()


def test_async_clients_wait_forever_unless_a_process_sets_a_limit(provider):
    provider.connect()
    for client in (provider.async_client, provider.async_bytes_client):
        assert client.connection_pool.connection_kwargs["socket_timeout"] is None


def test_a_read_on_a_silent_connection_gives_up(provider):
    async def main():
        async def swallow(reader, writer):
            await reader.read()  # until the client goes away; never answers

        server = await asyncio.start_server(swallow, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        provider.url = f"redis://127.0.0.1:{port}"
        provider.async_socket_timeout = 0.3
        provider.connect()

        started = time.monotonic()
        with pytest.raises(RedisTimeoutError):
            # Returns in the end, so a missing limit fails this test and not hangs it.
            await asyncio.wait_for(
                provider.async_client.xread({"stream": "$"}, block=100), 5
            )
        elapsed = time.monotonic() - started

        await provider.async_client.aclose()
        server.close()
        return elapsed

    assert asyncio.run(main()) < 3
