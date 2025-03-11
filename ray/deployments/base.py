

from typing import Optional
import socketio

from minio import Minio
from pydantic import BaseModel, ConfigDict
from ray import serve


from ...logging import load_logger


class BaseDeployment:

    def __init__(
        self,
        api_url: str,
        object_store_url: str,
        object_store_access_key: str,
        object_store_secret_key: str,
    ) -> None:

        super().__init__()

        self.api_url = api_url
        self.object_store_url = object_store_url
        self.object_store_access_key = object_store_access_key
        self.object_store_secret_key = object_store_secret_key

        self.object_store = Minio(
            self.object_store_url,
            access_key=self.object_store_access_key,
            secret_key=self.object_store_secret_key,
            secure=False,
        )

        self.sio = socketio.SimpleClient(reconnection_attempts=10)

        self.logger = load_logger(
            service_name=str(self.__class__), logger_name="ray.serve"
        )
        
        try:

            self.replica_context = serve.get_replica_context()
            
        except:
            
            self.replica_context = None


class BaseDeploymentArgs(BaseModel):

    model_config = ConfigDict(arbitrary_types_allowed=True)

    api_url: Optional[str] = None
    object_store_url: Optional[str] = None
    object_store_access_key: Optional[str] = None
    object_store_secret_key: Optional[str] = None

