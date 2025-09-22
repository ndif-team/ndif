import os
from typing import TYPE_CHECKING
import psycopg2
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_400_BAD_REQUEST

import logging
from .metrics import NetworkStatusMetric
from .schema import BackendRequestModel
from .types import API_KEY, MODEL_KEY, TIER

if TYPE_CHECKING:
    from fastapi import Request

    from .schema import BackendRequestModel

logger = logging.getLogger("ndif")

# TODO: Make this be derived from a base class

class AccountsDB:
    """Database class for accounts"""
    def __init__(self, host, port, database, user, password):
        self.conn = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
            connect_timeout=10
        )
        self.cur = self.conn.cursor()

    def __del__(self):
        self.cur.close()
        self.conn.close()

    def api_key_exists(self, key_id: API_KEY) -> bool:
        """Check if a key exists"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT EXISTS(SELECT 1 FROM keys WHERE key_id = %s)", (key_id,))
                result = cur.fetchone()
                return result[0] if result else False
        except Exception as e:
            logger.error(f"Error checking if key exists: {e}")
            self.conn.rollback()
            return False

    def model_id_from_key(self, key_id: MODEL_KEY) -> Optional[str]:
        """Get the model ID from a key ID"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT model_id FROM models WHERE model_key = %s", (key_id,))
                result = cur.fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting model ID from key ID: {e}")
            self.conn.rollback()
            return None

    def key_has_access_to_model(self, key_id: API_KEY, model_id: str) -> bool:
        """Check if a key has access to a model"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                SELECT EXISTS(
                    SELECT 1 FROM key_tier_assignments
                    JOIN model_tier_assignments ON key_tier_assignments.tier_id = model_tier_assignments.tier_id
                    WHERE key_tier_assignments.key_id = %s AND model_tier_assignments.model_id = %s
                )
                """, (key_id, model_id))
                result = cur.fetchone()
                return result[0] if result else False
        except Exception as e:
            logger.error(f"Error checking if key has access to model: {e}")
            self.conn.rollback()
            return False

    def tier_id_from_name(self, name: TIER) -> Optional[str]:
        """Get the tier ID from a tier name"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT tier_id FROM tiers WHERE name = %s", (str(name.value).lower(),))
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
                cur.execute("SELECT EXISTS(SELECT 1 FROM key_tier_assignments WHERE key_id = %s AND tier_id = %s)", (key_id, self.tier_id_from_name(TIER.TIER_HOTSWAP)))
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

    # TODO: There should be some form of caching here
    # TODO: I should reintroduce the user email check here (unless we choose not to migrate keys which are missing an email)        
    
    # Check if the API key exists and is valid
    if not api_key_store.api_key_exists(request.api_key):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Please visit https://login.ndif.us/ to create a new one.",
        )

    if api_key_store.key_has_hotswapping_access(request.api_key):
        request.hotswapping = True
        
    model_key = request.model_key.lower()
    # Get the model ID from the API key
    model_id = api_key_store.model_id_from_key(model_key)
    if not model_id:
        # Let them have access by default (to support future usecase of dynamic model loading)
        return

    # Check if the model has access to the API key
    if not api_key_store.key_has_access_to_model(request.api_key, model_id):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail=f"API key does not have authorization to access the requested model: {model_key}.",
        )