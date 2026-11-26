"""A key carries its own user_tags plus its user's: ``_KEY_QUERY`` on Postgres.

Runs against the compose stack's Postgres (``docker/postgres/init.sql``) on
localhost:5432 and skips without one. What it inserts is rolled back.
"""

import asyncio

import pytest

asyncpg = pytest.importorskip("asyncpg")

from ndif.services.api.auth import _KEY_QUERY

URL = "postgresql://admin:admin@localhost:5432/ndif"

_USER = "INSERT INTO users (email) VALUES ($1) RETURNING user_id"
_KEY = "INSERT INTO keys (user_id) VALUES ($1) RETURNING key_id"
_TAG = "INSERT INTO user_tags (name) VALUES ($1) ON CONFLICT DO NOTHING"
_ON_KEY = (
    "INSERT INTO key_user_tag_assignments "
    "SELECT $1::uuid, user_tag_id FROM user_tags WHERE name = $2"
)
_ON_USER = (
    "INSERT INTO user_tag_assignments "
    "SELECT $1::uuid, user_tag_id FROM user_tags WHERE name = $2"
)


async def _rows(key_tags, user_tags):
    """The ``user_tag`` column ``_KEY_QUERY`` returns for a key tagged like so."""
    try:
        conn = await asyncpg.connect(URL, timeout=3)
    except (OSError, asyncio.TimeoutError, asyncpg.PostgresError):
        return None
    transaction = conn.transaction()
    await transaction.start()
    try:
        for name in ("test-a", "test-b", "test-other"):
            await conn.execute(_TAG, name)
        user = await conn.fetchval(_USER, "tags@test.invalid")
        key = await conn.fetchval(_KEY, user)
        await conn.executemany(_ON_KEY, [(key, name) for name in key_tags])
        await conn.executemany(_ON_USER, [(user, name) for name in user_tags])
        # Someone else's tags, on their user and on their key, stay theirs.
        other = await conn.fetchval(_USER, "other@test.invalid")
        await conn.execute(_ON_USER, other, "test-other")
        await conn.execute(_ON_KEY, await conn.fetchval(_KEY, other), "test-other")
        return [row["user_tag"] for row in await conn.fetch(_KEY_QUERY, key)]
    finally:
        await transaction.rollback()
        await conn.close()


@pytest.mark.parametrize(
    "key_tags, user_tags, expected",
    [
        ([], [], [None]),  # a known key with no tags is still one row
        (["test-a"], [], ["test-a"]),
        ([], ["test-b"], ["test-b"]),
        (["test-a"], ["test-b"], ["test-a", "test-b"]),
        (["test-a"], ["test-a"], ["test-a"]),  # granted twice, returned once
    ],
)
def test_a_key_carries_its_own_tags_and_its_users(key_tags, user_tags, expected):
    rows = asyncio.run(_rows(key_tags, user_tags))
    if rows is None:
        pytest.skip(f"no Postgres at {URL}")
    assert sorted(rows, key=str) == expected
