import os
import logging

from .types import API_KEY, TIER
from .providers.postgres import PostgresProvider

logger = logging.getLogger("ndif")

DEV_MODE = os.environ.get("NDIF_DEV_MODE", "false").lower() == "true"


class AccountsDB:
    """Database class for accounts using PostgresProvider connection pool."""

    def api_key_exists(self, key_id: API_KEY) -> bool:
        """Check if a key exists. Assumes key_id has already been validated as a UUID."""
        try:
            result = PostgresProvider.execute(
                "SELECT EXISTS(SELECT 1 FROM keys WHERE key_id = %s)",
                (key_id,),
                fetchone=True,
            )
            return result[0] if result else False
        except Exception as e:
            logger.error(f"Error checking if key exists: {e}")
            return False

    def tier_id_from_name(self, name: TIER) -> str | None:
        """Get the tier ID from a tier name."""
        try:
            result = PostgresProvider.execute(
                "SELECT tier_id FROM tiers WHERE name = %s",
                (str(name.value).lower(),),
                fetchone=True,
            )
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting tier ID from tier name: {e}")
            return None

    def key_has_hotswapping_access(self, key_id: API_KEY) -> bool:
        """Check if a key has hotswapping access."""
        try:
            result = PostgresProvider.execute(
                "SELECT EXISTS(SELECT 1 FROM key_tier_assignments WHERE key_id = %s AND tier_id = %s)",
                (key_id, self.tier_id_from_name(TIER.TIER_HOTSWAP)),
                fetchone=True,
            )
            return result[0] if result else False
        except Exception as e:
            logger.error(f"Error checking if key has hotswapping access: {e}")
            return False


api_key_store = None
if not DEV_MODE:
    PostgresProvider.from_env()
    PostgresProvider.connect()
    api_key_store = AccountsDB()
