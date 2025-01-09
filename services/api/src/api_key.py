import os

import uuid
import firebase_admin
from cachetools import TTLCache, cached
from datetime import datetime
from fastapi import HTTPException, Request
from firebase_admin import credentials, firestore
from starlette.status import HTTP_401_UNAUTHORIZED

from .schema import BackendRequestModel
from .metrics import NDIFGauge

gauge = NDIFGauge(service='app')

llama_405b = 'nnsight.models.LanguageModel.LanguageModel:{"repo_id": "meta-llama/Meta-Llama-3.1-405B"}'

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

def extract_request_metadata(raw_request: Request) -> dict:
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

def init_request(request: BackendRequestModel) -> BackendRequestModel:
    """
    Initializes a BackendRequestModel by setting the ID, and received timestamp.
    """
    # Ensure request ID and received timestamp are set
    if not request.id:
        request.id = str(uuid.uuid4())
    if not request.received:
        request.received = datetime.now()
    return request

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

    # Extract metadata from the raw request
    metadata = extract_request_metadata(raw_request)

    ip_address, user_agent, content_length = metadata.values()
    gauge.update_network(request.id, ip_address, user_agent, content_length)

    if FIREBASE_CREDS_PATH is not None:
        check_405b = False
        if request.model_key == llama_405b:
            check_405b = True
        
        doc = api_key_store.fetch_document(request.api_key)
        if not api_key_store.does_api_key_exist(doc, check_405b):
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED, detail="Missing or invalid API key"
            )
