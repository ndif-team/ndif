import os

import firebase_admin
from cachetools import TTLCache, cached
from typing import TYPE_CHECKING
from firebase_admin import credentials, firestore
from starlette.status import HTTP_401_UNAUTHORIZED
from fastapi import HTTPException

from .schema import BackendRequestModel
from .metrics import NDIFGauge
from .util import check_valid_email
from .logging import load_logger

if TYPE_CHECKING:
    from fastapi import Request
    from .schema import BackendRequestModel

logger = load_logger(service_name='api', logger_name='gunicorn.error')
gauge = NDIFGauge(service='app')

llama_405b = 'nnsight.modeling.language.LanguageModel:{"repo_id": "meta-llama/Meta-Llama-3.1-405B"}'

class ApiKeyStore:

    def __init__(self, cred_path: str) -> None:

        self.cred = credentials.Certificate(cred_path)

        try:

            self.app = firebase_admin.get_app()
        except ValueError as e:

            firebase_admin.initialize_app(self.cred)

        self.firestore_client = firestore.client()

    @cached(cache=TTLCache(maxsize=1024, ttl=60))
    def fetch_document(self, api_key: str):

        doc = self.firestore_client.collection("keys").document(api_key).get()
        return doc

    def does_api_key_exist(self, doc, check_405b: bool = False) -> bool:

        return doc.exists and doc.to_dict().get('tier') == '405b' if check_405b else doc.exists

    def get_uid(self, doc):
        user_id = doc.to_dict().get('user_id')
        return user_id

FIREBASE_CREDS_PATH = os.environ.get("FIREBASE_CREDS_PATH", None)

if FIREBASE_CREDS_PATH is not None:

    api_key_store = ApiKeyStore(FIREBASE_CREDS_PATH)

def extract_request_metadata(raw_request: "Request") -> dict:
    """
    Extracts relevant metadata from the incoming raw request, such as IP address,
    user agent, and content length, and returns them as a dictionary.
    """
    metadata = {
        'ip_address': raw_request.client.host,
        'user_agent': raw_request.headers.get('user-agent'),
        'content_length': int(raw_request.headers.get('content-length', 0))
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
    gauge.update_network(request.id, ip_address, user_agent, content_length)

    if FIREBASE_CREDS_PATH is not None:
        check_405b = True if request.model_key == llama_405b else False
        
        doc = api_key_store.fetch_document(request.api_key)

        # Check if the API key exists and is valid
        if api_key_store.does_api_key_exist(doc, check_405b):
            # Check if the document contains a valid email
            user_id = api_key_store.get_uid(doc)
            logger.info(user_id)
            if not check_valid_email(user_id):
                # Handle case where API key exists but doesn't contain a valid email
                raise HTTPException(
                    status_code=HTTP_401_UNAUTHORIZED,
                    detail="Invalid API key: A valid API key must contain an email. Please visit https://login.ndif.us/ to create a new one."
                )
        else:
            # Handle case where API key does not exist or is invalid
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API key. Please visit https://login.ndif.us/ to create a new one."
            )
