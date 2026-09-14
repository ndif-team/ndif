"""API-key authentication against the Postgres user/keys database.

This is the API service's auth layer over :class:`~ndif.common.providers.postgres.PostgresProvider`.
The provider is a generic async connection pool; the *policy* lives here — what a
valid key looks like, and which HTTP status each failure maps to.

Auth is **optional** and gated on Postgres being configured
(``NDIF_POSTGRES_URL``). When it isn't set, :func:`verify_api_key` is a no-op and
every request is allowed — the ``api_key`` is then only carried for telemetry,
which is the pre-auth behavior. When it *is* set, a request must present a key
that exists in the ``keys`` table (see ``docker/postgres/init.sql``):

    missing key              -> 401  (client didn't send a key)
    malformed key            -> 400  (present but not a UUID)
    key not in the DB        -> 403  (key is well-formed but not authorized)
    DB unreachable / errored -> 503  (fail closed — never allow on backend error)

The DB path fails *closed*: if Postgres is enabled but unreachable, requests are
rejected rather than let through unverified.

**Validity criterion**: a key is valid iff its row exists in ``keys``. The
owning user's ``verification_status`` is deliberately *not* enforced here — key
issuance/gating is the login page's job, so the API just checks the key is known.
All queries are ``SELECT`` only: the API connects as the read-only ``ndifapi``
role.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from fastapi import Form, Header, HTTPException
from pydantic import ValidationError

from ndif.common.providers.postgres import PostgresProvider
from ndif.common.schema.request import BackendRequestModel

logger = logging.getLogger("ndif.api")

# A key granted this user_tag may run its request without sandbox isolation (and
# deploy the model with trust_remote_code); see Identity.trusted.
TRUSTED_TAG = "trusted"

# A key granted this user_tag jumps the queue: its requests are prepended to the
# front of the model's queue rather than appended; see Identity.priority.
PRIORITY_TAG = "priority"

# One row per user_tag granted to the key; no rows means the key doesn't exist.
# The LEFT JOINs mean a key with no tag assignments still returns a single row
# (with tag = NULL), distinguishing "known key, no tags" from "unknown key".
# ``user_id`` / ``email`` (the key's owner) are the same on every row; ``email``
# is LEFT JOINed so a key whose user row is missing still verifies (email NULL).
_KEY_QUERY = """
SELECT k.user_id, u.email, ut.name AS user_tag
FROM keys k
LEFT JOIN users u ON u.user_id = k.user_id
LEFT JOIN key_user_tag_assignments kuta ON kuta.key_id = k.key_id
LEFT JOIN user_tags ut ON ut.user_tag_id = kuta.user_tag_id
WHERE k.key_id = $1
"""


@dataclass
class Identity:
    """The authenticated caller: their key, its owning user, and its user_tags."""

    key_id: str
    user_id: str
    email: Optional[str] = None
    user_tags: List[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        """Whether this key carries the ``trusted`` user_tag."""
        return TRUSTED_TAG in self.user_tags

    @property
    def priority(self) -> bool:
        """Whether this key carries the ``priority`` user_tag."""
        return PRIORITY_TAG in self.user_tags


async def verify_api_key(api_key: Optional[str]) -> Optional[Identity]:
    """Verify an API key against the Postgres keys DB.

    Returns the caller's :class:`Identity` on success, or ``None`` when auth is
    disabled (no Postgres configured). Raises :class:`~fastapi.HTTPException`
    (401 / 403 / 503) on any failure, so callers can simply ``await`` it and let
    a bad key short-circuit the request.
    """
    # Auth disabled: Postgres isn't configured, so allow everything.
    if not PostgresProvider.enabled():
        return None

    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid API key. Please visit "
            "https://login.ndif.us/ to create a new one.",
        )

    # Keys are UUIDs (keys.key_id is a UUID column). Reject anything that isn't
    # one before touching the DB — asyncpg would raise on a bad UUID param anyway.
    try:
        key_id = uuid.UUID(api_key)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid API key format: '{api_key}'. API keys must be in "
            "the format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx. You can obtain a "
            "valid API key from https://login.ndif.us",
        )

    try:
        rows = await PostgresProvider.fetch(_KEY_QUERY, key_id)
    except HTTPException:
        raise
    except Exception:
        # Fail closed: a DB outage must not let unverified requests through.
        logger.warning(
            "API-key verification failed: Postgres unavailable",
            exc_info=True,
        )
        raise HTTPException(status_code=503, detail="Auth backend unavailable.")

    if not rows:
        raise HTTPException(status_code=403, detail="Invalid API key.")

    user_tags = [row["user_tag"] for row in rows if row["user_tag"] is not None]
    return Identity(
        key_id=str(key_id),
        user_id=str(rows[0]["user_id"]),
        email=rows[0]["email"],
        user_tags=user_tags,
    )


async def validate_request(
    data: str = Form(..., description="JSON-encoded BackendRequestModel data"),
    api_key: Optional[str] = Header(default=None, alias="ndif-api-key"),
) -> BackendRequestModel:
    """FastAPI dependency: parse the request envelope and authenticate the caller.

    Builds the :class:`BackendRequestModel` from the multipart ``data`` (422 on a
    bad envelope), carries the ``ndif-api-key`` header onto it (the client sends
    the key there, not in the JSON), verifies it against the Postgres keys DB
    (raising 401/400/403/503 on failure, short-circuiting before enqueue), and
    stamps the caller's ``email``, ``trusted``, and ``priority`` flags onto the
    request so they travel with it through the queue.

    ``trusted`` and ``priority`` come from the key's ``trusted`` / ``priority``
    user_tags. With auth off (no Postgres configured) the deployment is assumed to
    sit on a trusted network: a client-supplied ``trusted`` is honored (so a caller
    can opt into the sandbox path with ``trusted=False``), and when the client
    leaves it unspecified it defaults to True. ``email`` and ``priority`` from the
    client are left as sent in that mode.
    """
    try:
        request = BackendRequestModel.model_validate_json(data)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid JSON: {exc}") from exc

    # Whether the client put ``trusted`` in the envelope, distinguished from the
    # field's default so the auth-off branch can tell "unspecified" from an
    # explicit ``trusted=False``.
    client_set_trusted = "trusted" in request.model_fields_set

    if api_key:
        request.api_key = api_key

    identity = await verify_api_key(request.api_key)
    if identity is not None:
        request.email = identity.email
        request.trusted = identity.trusted
        request.priority = identity.priority
    elif not client_set_trusted:
        # Auth is off (NDIF_POSTGRES_URL unset): a trusted-network / dev mode.
        # Default to trusted when the client didn't ask; an explicit request
        # value (True or False) is left untouched.
        request.trusted = True

    return request
