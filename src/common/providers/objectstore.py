import os
import logging
import boto3

from . import Provider

logger = logging.getLogger("ndif")


class ObjectStoreProvider(Provider):
    object_store_service: str
    object_store_url: str
    object_store_bucket: str
    object_store_access_key: str
    object_store_secret_key: str
    object_store_region: str
    object_store_verify: bool
    object_store: boto3.client

    @classmethod
    def from_env(cls):
        super().from_env()
        cls.object_store_service = os.environ.get("NDIF_OBJECT_STORE_SERVICE", "s3")
        cls.object_store_url = os.environ.get(
            "NDIF_OBJECT_STORE_URL", "http://localhost:27018"
        )
        cls.object_store_bucket = os.environ.get(
            "NDIF_OBJECT_STORE_BUCKET", "ndif-results"
        )
        cls.object_store_access_key = os.environ.get(
            "NDIF_OBJECT_STORE_ACCESS_KEY", "minioadmin"
        )
        cls.object_store_secret_key = os.environ.get(
            "NDIF_OBJECT_STORE_SECRET_KEY", "minioadmin"
        )
        cls.object_store_region = os.environ.get(
            "NDIF_OBJECT_STORE_REGION", "us-east-1"
        )
        cls.object_store_verify = (
            os.environ.get("NDIF_OBJECT_STORE_VERIFY", "false").lower() == "true"
        )

    @classmethod
    def to_env(cls):
        return {
            **super().to_env(),
            "NDIF_OBJECT_STORE_SERVICE": cls.object_store_service,
            "NDIF_OBJECT_STORE_URL": cls.object_store_url,
            "NDIF_OBJECT_STORE_BUCKET": cls.object_store_bucket,
            "NDIF_OBJECT_STORE_ACCESS_KEY": cls.object_store_access_key,
            "NDIF_OBJECT_STORE_SECRET_KEY": cls.object_store_secret_key,
            "NDIF_OBJECT_STORE_REGION": cls.object_store_region,
            "NDIF_OBJECT_STORE_VERIFY": str(cls.object_store_verify).lower(),
        }

    @classmethod
    def connect(cls):
        logger.info(f"Connecting to object store at {cls.object_store_url}...")
        cls.object_store = boto3.client(
            cls.object_store_service,
            endpoint_url=cls.object_store_url,
            aws_access_key_id=cls.object_store_access_key,
            aws_secret_access_key=cls.object_store_secret_key,
            region_name=cls.object_store_region,
            verify=cls.object_store_verify,
        )
        logger.info("Connected to object store")


ObjectStoreProvider.from_env()
