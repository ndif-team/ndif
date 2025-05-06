import os
from typing import TYPE_CHECKING
import psycopg2
from typing import Optional

#import firebase_admin
#from cachetools import TTLCache, cached
from fastapi import HTTPException
#from firebase_admin import credentials, firestore
from starlette.status import HTTP_401_UNAUTHORIZED

from .logging import load_logger
from .metrics import NetworkStatusMetric
from .schema import BackendRequestModel
#from .util import check_valid_email

if TYPE_CHECKING:
    from fastapi import Request

    from .schema import BackendRequestModel

logger = load_logger(service_name="api", logger_name="gunicorn.error")


#llama_405b = 'nnsight.modeling.language.LanguageModel:{"repo_id": "meta-llama/Meta-Llama-3.1-405B"}'


# class ApiKeyStore:

#     def __init__(self, cred_path: str) -> None:

#         self.cred = credentials.Certificate(cred_path)

#         try:

#             self.app = firebase_admin.get_app()
#         except ValueError as e:

#             firebase_admin.initialize_app(self.cred)

#         self.firestore_client = firestore.client()

#     @cached(cache=TTLCache(maxsize=1024, ttl=60))
#     def fetch_document(self, api_key: str):

#         doc = self.firestore_client.collection("keys").document(api_key).get()
#         return doc

#     def does_api_key_exist(self, doc, check_405b: bool = False) -> bool:

#         return (
#             doc.exists and doc.to_dict().get("tier") == "405b"
#             if check_405b
#             else doc.exists
#         )

#     def get_uid(self, doc):
#         user_id = doc.to_dict().get("user_id")
#         return user_id

# TODO: Make this be derived from a base class

class AccountsDB:
    """Database class for accounts"""
    def __init__(self, host, port, database, user, password):
        self.conn = psycopg2.connect(
            host=host,
            port=port,
            database=database,
            user=user,
            password=password
        )
        self.cur = self.conn.cursor()

    def __del__(self):
        self.cur.close()
        self.conn.close()

    def api_key_exists(self, key_id: str) -> bool:
        """Check if a key exists"""
        self.cur.execute("SELECT EXISTS(SELECT 1 FROM keys WHERE key_id = %s)", (key_id,))
        result = self.cur.fetchone()
        return result[0] if result else None

    def model_id_from_key(self, key_id: str) -> Optional[str]:
        """Get the model ID from a key ID"""
        self.cur.execute("SELECT model_id FROM models WHERE checkpoint_id = %s", (key_id,))
        result = self.cur.fetchone()
        return result[0] if result else None

    def key_has_access_to_model(self, key_id: str, model_id: str) -> bool:
        """Check if a key has access to a model"""
        self.cur.execute("""
            SELECT * FROM key_tier_assignments
            JOIN model_tier_assignments ON key_tier_assignments.tier_id = model_tier_assignments.tier_id
            WHERE key_tier_assignments.key_id = %s AND model_tier_assignments.model_id = %s
        """, (key_id, model_id))
        result = self.cur.fetchone()
        return result is not None

    def validate_key_to_model(self, key_id: str, model_key: str) -> bool:
        """Check if a key has access to a model"""

        # First, check that the key exists
        if not self.api_key_exists(key_id):
            print(f"Key {key_id} does not exist")
            return False

        # Next, get the model ID from the key (and verify that it exists)
        model_id = self.model_id_from_key(model_key)
        if not model_id:
            print(f"Model {model_key} does not exist")
            return False

        # Finally, check that the key has a tier that allows access to the model
        return self.key_has_access_to_model(key_id, model_id)



#FIREBASE_CREDS_PATH = os.environ.get("FIREBASE_CREDS_PATH", None)

host = os.environ.get("POSTGRES_HOST")
port = os.environ.get("POSTGRES_PORT")
database = os.environ.get("POSTGRES_DATABASE")
user = os.environ.get("POSTGRES_USER")
password = os.environ.get("POSTGRES_PASSWORD")


#if FIREBASE_CREDS_PATH is not None:

#    api_key_store = ApiKeyStore(FIREBASE_CREDS_PATH)

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

    #check_405b = True if request.model_key == llama_405b else False
    
    # TODO: I probably should keep this flow the same as it was before for the time being.
    # TODO: There should be some form of caching here
    # TODO: I should reintroduce the user email check here (unless we choose not to migrate keys which are missing an email)        
    if not api_key_store.validate_key_to_model(request.api_key, request.model_key):

        #doc = api_key_store.fetch_document(request.api_key)

        # Check if the API key exists and is valid
        #if api_key_store.does_api_key_exist(doc, check_405b):
            # Check if the document contains a valid email
            #user_id = api_key_store.get_uid(doc)
            #logger.info(user_id)
            #if not check_valid_email(user_id):
                # Handle case where API key exists but doesn't contain a valid email
        #    raise HTTPException(
        #        status_code=HTTP_401_UNAUTHORIZED,
        #        detail="Invalid API key: A valid API key must contain an email. Please visit https://login.ndif.us/ to create a new one.",
        #    )
        #else:
            # Handle case where API key does not exist or is invalid


        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Please visit https://login.ndif.us/ to create a new one.",
        )
