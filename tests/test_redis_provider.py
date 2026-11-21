"""Which Redis clients retry a dead connection, and how many connections the
async ones may hold.

Pure config checks: the clients connect lazily, so no server is needed.
"""

from __future__ import annotations

from ndif.common.providers.redis import RedisProvider


def test_sync_client_retries():
    """``from_url`` doesn't get the retries ``redis.Redis()`` has by default, and
    a model actor writes its result through this client, so without them one dead
    connection loses a request that already finished."""
    retry = RedisProvider.sync_client.connection_pool.connection_kwargs["retry"]

    assert retry.get_retries() == 3


def test_async_clients_do_not_retry():
    """The API's websocket subscribes through these. A subscriber that reconnected
    silently would miss whatever was published meanwhile and wait forever, so a
    dropped connection has to surface as an error."""
    for client in (RedisProvider.async_client, RedisProvider.async_bytes_client):
        assert "retry" not in client.connection_pool.connection_kwargs


def test_async_clients_are_not_held_to_redis_pys_100_connections():
    """Every open ``/subscribe`` session holds one connection of its API
    worker's pool. At redis-py's default of 100 the 99th request got a 500 and
    a result that finished with the pool full never reached its client."""
    for client in (RedisProvider.async_client, RedisProvider.async_bytes_client):
        assert client.connection_pool.max_connections == 10_000
