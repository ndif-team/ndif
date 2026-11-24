"""The runtime limit: a user gets X minutes of model runtime every Y minutes.

X and Y are the one row of the Postgres table ``runtime_limit``, which the
dispatcher reads itself; with no row, or no Postgres, there is no limit. A
replica asks :func:`refusal` before it runs a request and calls :func:`charge`
with the time the run took. A running request is never stopped, so a user can
overdraw, and pays the overdraft back before their next request runs.

The budget refills continuously, X minutes per Y. A user's whole state is one
number in the Redis sorted set ``runtime``: the time their budget is back at
zero. It is in the future for a user who has overdrawn, and they are refused
until it passes. Running for ``s`` seconds moves it ``s * Y / X`` seconds later.
A budget that was at zero Y minutes ago is full, and gets no fuller.
"""

import asyncio
import logging
import math
import time
from typing import Optional, Tuple

from ....common.providers.postgres import PostgresProvider
from ....common.providers.redis import RedisProvider
from ....common.schema.request import BackendRequestModel

logger = logging.getLogger("ndif.queue.runtime_limit")

KEY = "runtime"
QUERY = "SELECT minutes, per_minutes FROM runtime_limit"
REFRESH_S = 30
READ_TIMEOUT_S = 5

_limit: Optional[Tuple[int, int]] = None
_read_at = 0.0


async def limit(request: BackendRequestModel) -> Optional[Tuple[int, int]]:
    """``(X, Y)`` for the request's user, or ``None`` for no limit.

    Postgres is read at most every ``REFRESH_S`` seconds. A request waits on
    that read, so it gets ``READ_TIMEOUT_S``, and one that fails keeps the last
    value.
    """
    global _limit, _read_at
    if not PostgresProvider.enabled() or not request.email or "unlimited" in request.tags:
        return None
    if time.time() - _read_at > REFRESH_S:
        # Set first, so replicas that ask during the read use the last value.
        _read_at = time.time()
        try:
            row = await asyncio.wait_for(PostgresProvider.fetchrow(QUERY), READ_TIMEOUT_S)
            _limit = (row["minutes"], row["per_minutes"]) if row else None
        except Exception as error:
            logger.warning(f"runtime limit not read: {type(error).__name__}: {error}")
    return _limit


async def refusal(request: BackendRequestModel) -> Optional[str]:
    """The error for a request whose user is out of runtime, else ``None``."""
    runtime = await limit(request)
    if runtime is None:
        return None
    zero_at = await RedisProvider.async_client.zscore(KEY, request.email) or 0
    wait = zero_at - time.time()
    if wait <= 0:
        return None
    minutes, per_minutes = runtime
    return (
        f"Runtime limit reached: your account gets {minutes} min of model "
        f"runtime every {per_minutes} min. Try again in {math.ceil(wait / 60)} min."
    )


async def charge(request: BackendRequestModel, seconds: float) -> None:
    """Take ``seconds`` of runtime out of the budget of the request's user."""
    runtime = await limit(request)
    if runtime is None:
        return
    minutes, per_minutes = runtime
    try:
        # One transaction. Replicas that finish a user's requests together would
        # otherwise each start from the old value, and all but one charge is lost.
        async with RedisProvider.async_client.pipeline() as pipe:
            # Full at most: a budget is never counted from further back than Y.
            pipe.zadd(KEY, {request.email: time.time() - per_minutes * 60}, gt=True)
            pipe.zincrby(KEY, seconds * per_minutes / minutes, request.email)
            await pipe.execute()
    except Exception as error:
        # The request already has its answer; raising here would send it an
        # error too. A lost charge only undercounts.
        logger.warning(f"runtime charge lost: {type(error).__name__}: {error}")
