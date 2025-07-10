import os
import logging
import boto3

from . import Provider

logger = logging.getLogger("ndif")

class ObjectStoreProvider(Provider):

    object_store_service: str
    object_store_url: str
    object_store_access_key: str
    object_store_secret_key: str
    object_store_region: str
    object_store_verify: bool
    object_store: boto3.client
    
    @classmethod
    def from_env(cls):
        super().from_env()
        cls.object_store_service = os.environ.get("OBJECT_STORE_SERVICE", "s3")
        cls.object_store_url = os.environ.get("OBJECT_STORE_URL", None)
        cls.object_store_access_key = os.environ.get("OBJECT_STORE_ACCESS_KEY", "minioadmin")
        cls.object_store_secret_key = os.environ.get("OBJECT_STORE_SECRET_KEY", "minioadmin")
        cls.object_store_region = os.environ.get("OBJECT_STORE_REGION", "us-east-1")
        cls.object_store_verify = os.environ.get("OBJECT_STORE_VERIFY", False)
    
    @classmethod 
    def to_env(cls):
        return {
            **super().to_env(),
            "OBJECT_STORE_SERVICE": cls.object_store_service,
            "OBJECT_STORE_URL": cls.object_store_url,
            "OBJECT_STORE_ACCESS_KEY": cls.object_store_access_key,
            "OBJECT_STORE_SECRET_KEY": cls.object_store_secret_key,
            "OBJECT_STORE_REGION": cls.object_store_region,
            "OBJECT_STORE_VERIFY": cls.object_store_verify,
        }
    
    @classmethod
    def connect(cls):
        logger.info(f"Connecting to object store at {cls.object_store_url}...")
        cls.object_store = boto3.client(
            cls.object_store_service,
            endpoint_url=f"http://{cls.object_store_url}",
            aws_access_key_id=cls.object_store_access_key,
            aws_secret_access_key=cls.object_store_secret_key,
            region_name=cls.object_store_region,
            verify=cls.object_store_verify,
        )
        logger.info("Connected to object store")

ObjectStoreProvider.from_env()