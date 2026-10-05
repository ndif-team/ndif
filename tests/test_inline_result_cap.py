"""The inline-result cap, derived from Redis's own pubsub limits.

A result small enough rides back on the COMPLETED response over a Redis pubsub
channel. The ceiling on that path is Redis itself: a subscriber whose output
buffer passes ``client-output-buffer-limit pubsub`` is *disconnected*, and the
buffered response is silently gone — the job reads COMPLETED while the result
is unrecoverable. So the cap is derived from the server's own limits
(``RedisProvider.max_publish_bytes``) rather than configured beside them, and
these tests pin the derivation and every fallback.

No server: the seam is ``RedisProvider.sync_client``, the singleton the real
derivation queries, swapped for a scripted stand-in. What ``CONFIG GET``
answers — including answering nothing, or refusing — is the whole input.
"""

from __future__ import annotations

import pytest

# No stack guard, like test_fanout.py: the derivation is pure parsing plus one
# (faked) server query, so this collects and runs anywhere the package imports.
from ndif.common.providers.redis import (
    STOCK_PUBSUB_SOFT_LIMIT_BYTES,
    UNLIMITED_PUBSUB_CEILING_BYTES,
    RedisProvider,
)

MiB = 1024 * 1024

# What a stock redis:7 answers: `client-output-buffer-limit pubsub 32mb 8mb 60`,
# flattened into one string alongside the other client classes.
STOCK = "normal 0 0 0 slave 268435456 67108864 60 pubsub 33554432 8388608 60"


class FakeRedis:
    """A scripted ``CONFIG GET client-output-buffer-limit``."""

    def __init__(self, value=STOCK, error=None):
        self.value = value
        self.error = error
        self.calls = 0

    def config_get(self, key):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.value is None:
            return {}  # blocked/renamed CONFIG on some managed Redis
        return {key: self.value}


@pytest.fixture
def cap(monkeypatch):
    """Install a scripted server, clear the memo, and return the derived cap."""

    def derive(value=STOCK, error=None):
        monkeypatch.setattr(RedisProvider, "sync_client", FakeRedis(value, error))
        monkeypatch.setattr(RedisProvider, "_max_publish_bytes", None)
        return RedisProvider.max_publish_bytes()

    return derive


class TestTheDerivation:
    """cap = min(soft, hard // 2), counting only the limits that are enabled."""

    def test_stock_config_caps_at_the_soft_limit(self, cap):
        # 8 MiB: min(8 MiB soft, 32 MiB hard // 2). The soft limit is the
        # primary bound — its omem (overhead factor measured up to ~1.67x) can
        # never reach the 32 MiB hard kill.
        assert cap(STOCK) == 8 * MiB == STOCK_PUBSUB_SOFT_LIMIT_BYTES

    def test_half_the_hard_limit_wins_when_it_is_smaller(self, cap):
        # soft 24 MiB, hard 32 MiB: a message under soft could still blow the
        # hard limit through omem overhead, so hard // 2 takes over.
        assert cap("pubsub 33554432 25165824 60") == 16 * MiB

    def test_a_disabled_soft_limit_falls_to_half_the_hard(self, cap):
        # 0 disables a limit in Redis; only the hard limit bounds the channel.
        assert cap("pubsub 33554432 0 0") == 16 * MiB

    def test_a_disabled_hard_limit_leaves_the_soft(self, cap):
        assert cap("pubsub 0 8388608 60") == 8 * MiB

    def test_no_limits_at_all_keeps_a_fixed_ceiling(self, cap):
        # Nothing would disconnect the subscriber, but an uncapped message
        # still buffers per subscriber in server memory: keep a sane ceiling.
        assert cap("pubsub 0 0 0") == 20 * MiB == UNLIMITED_PUBSUB_CEILING_BYTES

    def test_raising_the_redis_limits_raises_the_cap(self, cap):
        # The operator story: one source of truth. 128mb hard / 64mb soft.
        assert cap("pubsub 134217728 67108864 60") == 64 * MiB

    def test_a_bytes_reply_parses_the_same(self, cap):
        # decode_responses is this provider's doing, not a contract of the
        # seam; a raw-bytes answer must not land in the fallback.
        assert cap(STOCK.encode()) == 8 * MiB


class TestTheFallback:
    """Any failure to read the server's limits means a fixed conservative cap.

    8 MiB — the stock soft limit — because an uninspectable server might be
    running stock limits, and losing responses is the direction to guard
    against. Never uncapped, never an error.
    """

    def test_config_get_refused(self, cap):
        # ElastiCache and friends block or rename CONFIG.
        import redis as redis_py

        value = cap(error=redis_py.ResponseError("unknown command 'CONFIG'"))

        assert value == STOCK_PUBSUB_SOFT_LIMIT_BYTES

    def test_connection_failure(self, cap):
        assert cap(error=ConnectionError("down")) == STOCK_PUBSUB_SOFT_LIMIT_BYTES

    def test_an_empty_reply(self, cap):
        assert cap(value=None) == STOCK_PUBSUB_SOFT_LIMIT_BYTES

    def test_a_missing_pubsub_class(self, cap):
        assert cap("normal 0 0 0 slave 268435456 67108864 60") == (
            STOCK_PUBSUB_SOFT_LIMIT_BYTES
        )

    def test_a_malformed_value(self, cap):
        assert cap("pubsub lots some 60") == STOCK_PUBSUB_SOFT_LIMIT_BYTES

    def test_a_truncated_value(self, cap):
        assert cap("normal 0 0 0 pubsub") == STOCK_PUBSUB_SOFT_LIMIT_BYTES


class TestTheMemo:
    """Derived once per process; the decision runs on every request."""

    def test_the_server_is_asked_once(self, monkeypatch):
        fake = FakeRedis()
        monkeypatch.setattr(RedisProvider, "sync_client", fake)
        monkeypatch.setattr(RedisProvider, "_max_publish_bytes", None)

        first = RedisProvider.max_publish_bytes()
        second = RedisProvider.max_publish_bytes()

        assert first == second == 8 * MiB
        assert fake.calls == 1

    def test_a_failed_derivation_is_memoized_too(self, monkeypatch):
        # One warning, not one per request, against a Redis that refuses
        # CONFIG permanently (the common reason it fails at all).
        fake = FakeRedis(error=RuntimeError("no CONFIG here"))
        monkeypatch.setattr(RedisProvider, "sync_client", fake)
        monkeypatch.setattr(RedisProvider, "_max_publish_bytes", None)

        RedisProvider.max_publish_bytes()
        RedisProvider.max_publish_bytes()

        assert fake.calls == 1

    def test_reconnecting_forgets_the_memo(self, monkeypatch):
        # A new connection may be a different server with different limits.
        monkeypatch.setattr(RedisProvider, "sync_client", FakeRedis())
        monkeypatch.setattr(RedisProvider, "_max_publish_bytes", None)
        monkeypatch.setattr(RedisProvider, "async_client", None, raising=False)
        monkeypatch.setattr(
            RedisProvider, "async_bytes_client", None, raising=False
        )
        RedisProvider.max_publish_bytes()

        RedisProvider.connect()  # lazy clients: no socket until first command

        assert RedisProvider._max_publish_bytes is None
