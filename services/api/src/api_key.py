import os

import firebase_admin
from cachetools import TTLCache, cached
from fastapi import HTTPException, Security
from fastapi.security.api_key import APIKeyHeader
from firebase_admin import credentials, firestore
from starlette.status import HTTP_401_UNAUTHORIZED

from nnsight.schema.Request import RequestModel

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


async def api_key_auth(request : RequestModel, api_key: str = Security(api_key_header)):

    if FIREBASE_CREDS_PATH is not None:
        check_405b = False
        if request.model_key == 'nnsight.models.LanguageModel.LanguageModel:{"repo_id": "meta-llama/Meta-Llama-3.1-405B-Instruct"}':
            check_405b = True
        
        if api_key_store.does_api_key_exist(api_key, check_405b):
                return True

        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED, detail="Missing or invalid API key"
        )