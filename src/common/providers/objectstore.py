import os
import logging
import boto3

from . import Provider

logger = logging.getLogger("ndif")

class ObjectStoreProvider(Provider):

    object_store_service = os.environ.get("OBJECT_STORE_SERVICE", "s3")
    object_store_url = os.environ.get("OBJECT_STORE_URL", None)
    object_store_access_key = os.environ.get("OBJECT_STORE_ACCESS_KEY", "minioadmin")
    object_store_secret_key = os.environ.get("OBJECT_STORE_SECRET_KEY", "minioadmin")
    object_store_region = os.environ.get("OBJECT_STORE_REGION", "us-east-1")
    object_store_verify = os.environ.get("OBJECT_STORE_VERIFY", False)
    object_store: boto3.client = None
    
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