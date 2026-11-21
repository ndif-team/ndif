"""``require_ray_connection`` answers "not connected" for any trouble with Redis.

The guard reads one Redis key. A Redis that errors used to surface as a 500,
and one that never answered left the request hanging.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from redis.exceptions import ConnectionError as RedisConnectionError

pytest.importorskip("boto3", reason="the API imports the object store provider")

from ndif.services.api import app as api


class Redis:
    def __init__(self, answer):
        self.answer = answer

    async def get(self, key):
        assert key == "ray:connected"
        if isinstance(self.answer, Exception):
            raise self.answer
        if self.answer == "never":
            await asyncio.sleep(3600)
        return self.answer


def check(monkeypatch, answer):
    monkeypatch.setattr(api.RedisProvider, "async_client", Redis(answer), raising=False)
    # The check's own 5 s limit, shortened so the test does not wait it out.
    limit = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: limit(0.05))
    return asyncio.run(api.require_ray_connection())


def test_a_live_dispatcher_passes(monkeypatch):
    assert check(monkeypatch, "1") is None


@pytest.mark.parametrize(
    "answer",
    [None, RedisConnectionError("Redis went away"), "never"],
    ids=["key absent", "redis errors", "redis does not answer"],
)
def test_no_key_or_no_redis_is_a_503(monkeypatch, answer):
    with pytest.raises(HTTPException) as raised:
        check(monkeypatch, answer)

    assert raised.value.status_code == 503
