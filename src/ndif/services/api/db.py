import os
import logging

from ...common.types import API_KEY, TIER
from ...common.providers.postgres import PostgresProvider

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

    def get_email_from_key(self, key_id: API_KEY) -> str | None:
        """Return the email associated with a key, or None if not found."""
        try:
            result = PostgresProvider.execute(
                "SELECT u.email FROM keys k "
                "JOIN users u ON k.user_id = u.user_id "
                "WHERE k.key_id = %s",
                (key_id,),
                fetchone=True,
            )
            return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting email from key: {e}")
            return None

    def get_tiers_from_key(self, key_id: API_KEY) -> list[str]:
        """Return the list of tier names assigned to a key (empty if none)."""
        try:
            results = PostgresProvider.execute(
                "SELECT t.name FROM key_tier_assignments kta "
                "JOIN tiers t ON kta.tier_id = t.tier_id "
                "WHERE kta.key_id = %s",
                (key_id,),
            )
            return [row[0] for row in results] if results else []
        except Exception as e:
            logger.error(f"Error getting tiers from key: {e}")
            return []

    def key_has_tier(self, key_id: API_KEY, tier: TIER) -> bool:
        """Check if a key has been assigned a given tier."""
        try:
            result = PostgresProvider.execute(
                "SELECT EXISTS(SELECT 1 FROM key_tier_assignments WHERE key_id = %s AND tier_id = %s)",
                (key_id, self.tier_id_from_name(tier)),
                fetchone=True,
            )
            return result[0] if result else False
        except Exception as e:
            logger.error(f"Error checking if key has tier {tier}: {e}")
            return False

    def key_has_hotswapping_access(self, key_id: API_KEY) -> bool:
        """Check if a key may hotswap models.

        On the competition branch the ``tier_1`` tier gates both system access
        and hotswapping; there is no standalone hotswap tier.
        """
        return self.key_has_tier(key_id, TIER.TIER_1)


api_key_store = None
if not DEV_MODE:
    PostgresProvider.from_env()
    PostgresProvider.connect()
    api_key_store = AccountsDB()
