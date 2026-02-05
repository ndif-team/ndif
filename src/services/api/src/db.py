import os
import psycopg2
import logging
from typing import Optional

from .types import API_KEY, TIER

logger = logging.getLogger("ndif")

DEV_MODE = os.environ.get("NDIF_DEV_MODE", "false").lower() == "true"


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
        logger.debug(f"Checking if key {key_id} has hotswapping access")
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


api_key_store = None
if not DEV_MODE:
    # Initialize the database connection
    api_key_store = AccountsDB(
        os.environ.get("POSTGRES_HOST"),
        os.environ.get("POSTGRES_PORT"),
        os.environ.get("POSTGRES_DB"),
        os.environ.get("POSTGRES_USER"),
        os.environ.get("POSTGRES_PASSWORD"),
    )
