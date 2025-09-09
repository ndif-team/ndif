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

if TYPE_CHECKING:
    from fastapi import Request

    from .schema import BackendRequestModel

logger = logging.getLogger("ndif")

def parse_model_key(model_key: str) -> str:
    """
    Extracts the model ID (repo_id) from a model key string.

    Example:
        model_key = 'nnsight.modeling.language.languagemodel:{"repo_id": "meta-llama/Meta-Llama-3.1-8B", "revision": "main"}'
        returns 'meta-llama/meta-llama-3.1-8b'
    """
    import json

    try:
        # Split on the first colon to separate the prefix and the JSON part
        _, json_part = model_key.split(":", 1)
        # Parse the JSON part
        model_info = json.loads(json_part)
        # Return the repo_id if present
        return model_info.get("repo_id", "").lower()
    except Exception as e:
        logger.error(f"Failed to parse model_key '{model_key}': {e}")
        return ""

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

    def api_key_exists(self, key_id: str) -> bool:
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

    def model_id_from_key(self, key_id: str) -> Optional[str]:
        """Get the model ID from a key ID"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT model_id FROM tiered_models WHERE model_key = %s", (key_id,))
                result = cur.fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting model ID from key ID: {e}")
            self.conn.rollback()
            return None

    def key_has_access_to_model(self, key_id: str, model_id: str) -> bool:
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

    def model_is_whitelisted(self, model_key: str) -> bool:
        """Check if a model is whitelisted by matching any prefix at the start of the model_key"""
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM allowed_model_prefixes
                        WHERE %s LIKE CONCAT(prefix, '%%')
                    )
                    """,
                    (model_key,)
                )
                result = cur.fetchone()
                if result and result[0]:
                    return True
                return False
        except Exception as e:
            logger.error(f"Error checking if model is whitelisted: {e}")
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
        return

    # TODO: There should be some form of caching here
    # TODO: I should reintroduce the user email check here (unless we choose not to migrate keys which are missing an email)        
    
    # Check if the API key exists and is valid
    if not api_key_store.api_key_exists(request.api_key):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Please visit https://login.ndif.us/ to create a new one.",
        )
    model_key = parse_model_key(request.model_key)

  # Check gated
    model_id = api_key_store.model_id_from_key(model_key)
    if model_id:
        # Check if the model has access to the API key
        if not api_key_store.key_has_access_to_model(request.api_key, model_id):
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail=f"API key does not have authorization to access the requested model: {model_key}.",
            )

    # Now check whitelist
    if not api_key_store.model_is_whitelisted(model_key):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail=f"Model {model_key} is not whitelisted. Please contact info@ndif.us to request access.",
        )