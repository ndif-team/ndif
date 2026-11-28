"""The runtime limit, offline: no Postgres, no Redis, no Ray cluster.

The dispatcher reads the limit from Postgres; a replica refuses a request whose
user is out of runtime and charges the ones it runs.
"""

import asyncio
import time
import types

import pytest

pytest.importorskip("ray", reason="the replica imports Ray's exception classes")

from ndif.common.schema import Status
from ndif.common.schema.request import BackendRequestModel
from ndif.services.ray.queue import replica, runtime_limit

MINUTE = 60


class Redis:
    """The sorted set ``runtime``, and its own pipeline."""

    def __init__(self):
        self.scores = {}

    async def zscore(self, key, member):
        return self.scores.get(member)

    def pipeline(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *error):
        return None

    def zadd(self, key, mapping, gt):
        for member, score in mapping.items():
            self.scores[member] = max(score, self.scores.get(member, score))

    def zincrby(self, key, amount, member):
        self.scores[member] += amount

    async def execute(self):
        return None


@pytest.fixture
def redis(monkeypatch):
    redis = Redis()
    monkeypatch.setattr(runtime_limit.RedisProvider, "async_client", redis, raising=False)
    return redis


@pytest.fixture
def clock(monkeypatch):
    """A clock the test moves: ``clock.now += seconds``."""
    clock = types.SimpleNamespace(now=1_000_000.0)
    monkeypatch.setattr(time, "time", lambda: clock.now)
    return clock


@pytest.fixture(autouse=True)
def postgres(monkeypatch):
    """The ``runtime_limit`` table: ``postgres.row`` is its row, ``None`` for none."""
    postgres = types.SimpleNamespace(row={"minutes": 10, "per_minutes": 60}, reads=0)

    async def fetchrow(query):
        postgres.reads += 1
        if isinstance(postgres.row, Exception):
            raise postgres.row
        return postgres.row

    monkeypatch.setattr(runtime_limit.PostgresProvider, "url", "postgresql://db")
    monkeypatch.setattr(runtime_limit.PostgresProvider, "fetchrow", fetchrow)
    monkeypatch.setattr(runtime_limit, "_limit", None)
    monkeypatch.setattr(runtime_limit, "_read_at", 0.0)
    return postgres


def request(email="a@example.edu", tags=()):
    return BackendRequestModel(model_key="m", email=email, tags=set(tags))


def limit(**fields):
    return asyncio.run(runtime_limit.limit(request(**fields)))


def refusal(**fields):
    return asyncio.run(runtime_limit.refusal(request(**fields)))


def run(seconds, **fields):
    asyncio.run(runtime_limit.charge(request(**fields), seconds))


class TestTheBudget:
    def test_a_new_user_runs(self, redis, clock):
        assert refusal() is None

    def test_a_user_who_overdraws_is_refused_until_it_is_paid_back(self, redis, clock):
        run(11 * MINUTE)

        # One minute over, paid back at 10 minutes per 60.
        assert refusal() == (
            "Runtime limit reached: your account gets 10 min of model runtime "
            "every 60 min. Try again in 6 min."
        )
        clock.now += 6 * MINUTE
        assert refusal() is None

    def test_runs_at_once_add_up(self, redis, clock):
        for _ in range(3):
            run(4 * MINUTE)
        assert refusal() is not None

    def test_an_idle_user_does_not_save_up_more_than_a_full_budget(self, redis, clock):
        run(1 * MINUTE)
        clock.now += 24 * 60 * MINUTE
        run(11 * MINUTE)
        assert "Try again in 6 min" in refusal()

    def test_users_have_separate_budgets(self, redis, clock):
        run(11 * MINUTE)
        assert refusal(email="b@example.edu") is None

    def test_no_limit_is_never_counted(self, redis, clock, postgres):
        postgres.row = None
        run(11 * MINUTE)
        assert refusal() is None
        assert redis.scores == {}

    def test_a_charge_redis_refuses_is_dropped(self, redis, clock, monkeypatch):
        def unreachable(*args, **kwargs):
            raise ConnectionError("Redis went away")

        monkeypatch.setattr(redis, "zadd", unreachable)
        run(11 * MINUTE)
        assert refusal() is None


class TestTheReplica:
    """``Replica.dispatch`` with the actor and the response channel replaced."""

    @pytest.fixture
    def dispatch(self, redis, clock, monkeypatch):
        ran, responses = [], []

        async def actor_run(request):
            ran.append(request)
            clock.now += 11 * MINUTE

        async def arespond(self, status, description=""):
            responses.append((status, description))

        handle = types.SimpleNamespace(run=types.SimpleNamespace(remote=actor_run))
        monkeypatch.setattr(replica, "model_handle", lambda *key: handle)
        monkeypatch.setattr(BackendRequestModel, "arespond", arespond)
        worker = replica.Replica("m", "r", types.SimpleNamespace(queue=None))

        def dispatch():
            asyncio.run(worker.dispatch(request()))
            return ran, responses

        return dispatch

    def test_a_run_is_charged_the_time_it_took(self, dispatch):
        dispatch()
        assert "Try again in 6 min" in refusal()

    def test_a_refused_request_is_errored_and_never_reaches_the_actor(self, dispatch):
        dispatch()
        ran, responses = dispatch()

        assert len(ran) == 1
        status, description = responses[-1]
        assert status == Status.ERROR and description.startswith("Runtime limit reached")


class TestTheLimit:
    """What the dispatcher reads from the ``runtime_limit`` table."""

    def test_the_row_in_the_database(self, clock):
        assert limit() == (10, 60)
        assert limit(tags=["priority"]) == (10, 60)

    def test_none_when_there_is_no_row(self, clock, postgres):
        postgres.row = None
        assert limit() is None

    def test_none_without_postgres(self, clock, postgres, monkeypatch):
        monkeypatch.setattr(runtime_limit.PostgresProvider, "url", "")
        assert limit() is None
        assert postgres.reads == 0

    def test_none_for_a_user_tagged_unlimited(self, clock):
        assert limit(tags=["priority", "unlimited"]) is None

    def test_none_for_a_request_with_no_user(self, clock):
        assert limit(email=None) is None

    def test_a_change_is_read_within_the_refresh(self, clock, postgres):
        assert limit() == (10, 60)
        postgres.row = {"minutes": 30, "per_minutes": 60}
        assert limit() == (10, 60)

        clock.now += runtime_limit.REFRESH_S + 1
        assert limit() == (30, 60)
        assert postgres.reads == 2

    def test_a_read_that_fails_keeps_the_last_value(self, clock, postgres):
        assert limit() == (10, 60)
        postgres.row = ConnectionError("Postgres went away")

        clock.now += runtime_limit.REFRESH_S + 1
        assert limit() == (10, 60)

    def test_a_read_that_hangs_is_given_up(self, clock, monkeypatch):
        async def fetchrow(query):
            await asyncio.sleep(3600)

        monkeypatch.setattr(runtime_limit.PostgresProvider, "fetchrow", fetchrow)
        monkeypatch.setattr(runtime_limit, "READ_TIMEOUT_S", 0.01)
        assert limit() is None
