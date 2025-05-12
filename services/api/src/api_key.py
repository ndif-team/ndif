import os
from typing import TYPE_CHECKING
import psycopg2
from typing import Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_400_BAD_REQUEST

from .logging import load_logger
from .metrics import NetworkStatusMetric
from .schema import BackendRequestModel
import json
#from .util import check_valid_email

if TYPE_CHECKING:
    from fastapi import Request

    from .schema import BackendRequestModel

logger = load_logger(service_name="api", logger_name="gunicorn.error")

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

    def api_key_exists(self, key_id: str) -> bool:
        """Check if a key exists"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT EXISTS(SELECT 1 FROM keys WHERE key_id = %s)", (key_id,))
                result = cur.fetchone()
                return True if result else False
        except Exception as e:
            logger.error(f"Error checking if key exists: {e}")
            self.conn.rollback()
            return False
        else:
            self.conn.commit()

    def model_id_from_key(self, key_id: str) -> Optional[str]:
        """Get the model ID from a key ID"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT model_id FROM models WHERE checkpoint_id = %s", (key_id,))
                result = cur.fetchone()
                return result[0] if result else None
        except Exception as e:
            logger.error(f"Error getting model ID from key ID: {e}")
            self.conn.rollback()
            return None
        else:
            self.conn.commit()

    def key_has_access_to_model(self, key_id: str, model_id: str) -> bool:
        """Check if a key has access to a model"""
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                SELECT * FROM key_tier_assignments
                JOIN model_tier_assignments ON key_tier_assignments.tier_id = model_tier_assignments.tier_id
                WHERE key_tier_assignments.key_id = %s AND model_tier_assignments.model_id = %s
                """, (key_id, model_id))
                result = cur.fetchone()
                return result is not None
        except Exception as e:
            logger.error(f"Error checking if key has access to model: {e}")
            self.conn.rollback()
            return False
        else:
            self.conn.commit()



host = os.environ.get("POSTGRES_HOST")
port = os.environ.get("POSTGRES_PORT")
database = os.environ.get("POSTGRES_DB")
user = os.environ.get("POSTGRES_USER")
password = os.environ.get("POSTGRES_PASSWORD")


# Currently forcing this to be required so that it doesn't silently fail
api_key_store = AccountsDB(host, port, database, user, password)

def extract_request_metadata(raw_request: "Request") -> dict:
    """
    Extracts relevant metadata from the incoming raw request, such as IP address,
    user agent, and content length, and returns them as a dictionary.
    """
    metadata = {
        "ip_address": raw_request.client.host,
        "user_agent": raw_request.headers.get("user-agent"),
        "content_length": int(raw_request.headers.get("content-length", 0)),
    }
    return metadata


def api_key_auth(
    raw_request: "Request",
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

    metadata = extract_request_metadata(raw_request)

    ip_address, user_agent, content_length = metadata.values()
    NetworkStatusMetric.update(request.id, ip_address, user_agent, content_length)

    # For local development, we don't want to check the API key
    if host is None:
        return

    # TODO: There should be some form of caching here
    # TODO: I should reintroduce the user email check here (unless we choose not to migrate keys which are missing an email)        
    json_part = request.model_key.split(":", 1)[1]
    model_key = json.loads(json_part)["repo_id"]
    
    # Check if the API key exists and is valid
    if not api_key_store.api_key_exists(request.api_key):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Please visit https://login.ndif.us/ to create a new one.",
        )

    model_id = api_key_store.model_id_from_key(model_key)
    if not model_id:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Invalid model key. Please visit https://nnsight.net/status/ to see the list of available models.",
        )

    # Check if the model has access to the API key
    if not api_key_store.key_has_access_to_model(request.api_key, model_id):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail=f"API key does not have authorization to access the requested model: {model_key}.",
        )