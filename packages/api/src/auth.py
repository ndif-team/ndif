from functools import lru_cache
import os
from typing import TYPE_CHECKING
import psycopg2
from typing import Optional

from fastapi import HTTPException
from starlette.status import HTTP_401_UNAUTHORIZED

import logging
from common.schema import BackendRequestModel
from common.types import API_KEY, TIER

if TYPE_CHECKING:
    from common.schema import BackendRequestModel

logger = logging.getLogger("ndif")


class AccountsDB:
    """Database class for accounts"""

    def __init__(self, host, port, database, user, password):
        self.conn = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            connect_timeout=10,
        )
        self.cur = self.conn.cursor()

    def __del__(self):
        self.cur.close()
        self.conn.close()

    @lru_cache(maxsize=1000)
    def api_key_exists(self, key_id: API_KEY) -> bool:
        """Check if a key exists"""
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM keys WHERE key_id = %s)", (key_id,)
                )
                result = cur.fetchone()
                return result[0] if result else False
        except Exception as e:
            logger.error(f"Error checking if key exists: {e}")
            self.conn.rollback()
            return False

    def tier_id_from_name(self, name: TIER) -> Optional[str]:
        """Get the tier ID from a tier name"""
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT tier_id FROM tiers WHERE name = %s",
                    (str(name.value).lower(),),
                )
                result = cur.fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting tier ID from tier name: {e}")
            self.conn.rollback()
            return None

    def key_has_hotswapping_access(self, key_id: API_KEY) -> bool:
        """Check if a key has hotswapping access"""
        print(f"Checking if key {key_id} has hotswapping access")
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT EXISTS(SELECT 1 FROM key_tier_assignments WHERE key_id = %s AND tier_id = %s)",
                    (key_id, self.tier_id_from_name(TIER.TIER_HOTSWAP)),
                )
                result = cur.fetchone()
                return result[0] if result else False
        except Exception as e:
            logger.error(f"Error checking if key has hotswapping access: {e}")
            self.conn.rollback()
            return False


host = os.environ.get("POSTGRES_HOST")
port = os.environ.get("POSTGRES_PORT")
database = os.environ.get("POSTGRES_DB")
user = os.environ.get("POSTGRES_USER")
password = os.environ.get("POSTGRES_PASSWORD")


api_key_store = None
if host is not None:
    api_key_store = AccountsDB(host, port, database, user, password)


def api_key_auth(
    request: "BackendRequestModel",
) -> None:
    """
    Authenticates the API request by extracting metadata and initializing the BackendRequestModel
    with relevant information, including API key, client details, and headers.

    Args:
        - raw_request (Request): user request.
        - request (BackendRequestModel): user request object.

    Returns:
    """

    # For local development, we don't want to check the API key
    if host is None:
        request.hotswapping = True
        return

    # Check if the API key exists and is valid
    if not api_key_store.api_key_exists(request.api_key):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Please visit https://login.ndif.us/ to create a new one.",
        )

    if api_key_store.key_has_hotswapping_access(request.api_key):
        request.hotswapping = True
