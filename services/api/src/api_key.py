import os

import firebase_admin
from bson.objectid import ObjectId
from cachetools import TTLCache, cached
from datetime import datetime
from fastapi import HTTPException, Request, Security
from fastapi.security.api_key import APIKeyHeader
from firebase_admin import credentials, firestore
from starlette.status import HTTP_401_UNAUTHORIZED

from nnsight.schema.Response import ResponseModel
from .schema import BackendRequestModel
from .metrics import NDIFGauge

gauge = NDIFGauge(service='app')

llama_405b = 'nnsight.models.LanguageModel.LanguageModel:{"repo_id": "meta-llama/Meta-Llama-3.1-405B-Instruct"}'

class ApiKeyStore:

    def __init__(self, cred_path: str) -> None:

        self.cred = credentials.Certificate(cred_path)

        try:

            self.app = firebase_admin.get_app()
        except ValueError as e:

            firebase_admin.initialize_app(self.cred)

        self.firestore_client = firestore.client()

    @cached(cache=TTLCache(maxsize=1024, ttl=60))
    def does_api_key_exist(self, api_key: str, check_405b: bool = False) -> bool:

        doc = self.firestore_client.collection("keys").document(api_key).get()

        return doc.exists and doc.to_dict().get('tier') == '405b' if check_405b else doc.exists


FIREBASE_CREDS_PATH = os.environ.get("FIREBASE_CREDS_PATH", None)

if FIREBASE_CREDS_PATH is not None:

    api_key_store = ApiKeyStore(FIREBASE_CREDS_PATH)

api_key_header = APIKeyHeader(name="ndif-api-key", auto_error=False)

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
        request.id = str(ObjectId())
    if not request.received:
        request.received = datetime.now()
    return request

async def api_key_auth(
    request: BackendRequestModel,
    raw_request: Request,
    api_key: str = Security(api_key_header)
):
    """
    Authenticates the API request by extracting metadata and initializing the
    BackendRequestModel with relevant information, including API key, client details, and headers.
    """
    # Extract metadata from the raw request
    metadata = extract_request_metadata(raw_request)

    # TODO: Update the RequestModel to include additional fields (e.g. API key)
    request = init_request(request)

    gauge.update(request, api_key, ResponseModel.JobStatus.RECEIVED)

    ip_address, user_agent, content_length = metadata.values()
    gauge.update_network(request.id, ip_address, user_agent, content_length)

    if FIREBASE_CREDS_PATH is not None:
        check_405b = False
        if request.model_key == llama_405b:
            check_405b = True
        
        if api_key_store.does_api_key_exist(api_key, check_405b):
            gauge.update(request, api_key, ResponseModel.JobStatus.APPROVED)
            return request

        else:
            gauge.update(request, api_key, ResponseModel.JobStatus.ERROR)
            raise HTTPException(
                status_code=HTTP_401_UNAUTHORIZED, detail="Missing or invalid API key"
            )

    return request