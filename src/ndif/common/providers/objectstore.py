"""Object-store provider: an S3-compatible bucket for result blobs.

Uses **boto3**, so the same code talks to MinIO in dev and AWS S3 in prod.

Results are too big to ride the redis pub/sub response channel, so the model
actor uploads them here and publishes a presigned GET url on the COMPLETED
response; the client downloads them directly.

Two endpoints, because upload and download happen from different networks:
    ``url``         reached by the server (e.g. ``minio:9000`` on the compose
                    network) — used to upload and ensure the bucket. Leave empty
                    for real AWS S3 (boto3 derives the endpoint from ``region``).
    ``public_url``  reached by the client (e.g. ``localhost:9000`` on the host)
                    — used to *sign* GET urls. A presigned url is a local HMAC
                    over the request (host included), so it must be signed with
                    the host the downloader will actually hit. Defaults to
                    ``url`` when unset.
"""

import logging
from datetime import timedelta
from typing import Optional

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .base import Provider

logger = logging.getLogger("ndif")


def _boolish(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


class ObjectStoreProvider(Provider):
    CONFIG = {
        # Server-side endpoint. Empty -> boto3 derives the real AWS S3 endpoint
        # from `region` (prod); set to e.g. http://minio:9000 for dev.
        "url": ("NDIF_OBJECT_STORE_URL", "http://localhost:9000", str),
        # Client-facing endpoint used only for presigning. Empty -> use `url`.
        "public_url": ("NDIF_OBJECT_STORE_PUBLIC_URL", "", str),
        # Static keys, for a store that has no other way to authenticate — the
        # default pair is MinIO's. Set both empty to authenticate as whatever
        # role the host already carries; see _make_client.
        "access_key": ("NDIF_OBJECT_STORE_ACCESS_KEY", "minioadmin", str),
        "secret_key": ("NDIF_OBJECT_STORE_SECRET_KEY", "minioadmin", str),
        "bucket": ("NDIF_OBJECT_STORE_BUCKET", "ndif-results", str),
        # Set explicitly so presigning never round-trips to discover the region
        # (the public endpoint isn't reachable from the server's network).
        "region": ("NDIF_OBJECT_STORE_REGION", "us-east-1", str),
        # TLS cert verification; set false for self-signed MinIO over https.
        "verify": ("NDIF_OBJECT_STORE_VERIFY", True, _boolish),
    }

    url: str
    public_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str
    verify: bool

    client: "boto3.client"  # server-side: upload + bucket ops
    public_client: "boto3.client"  # client-facing: presign GET urls
    _bucket_ready: bool = False

    @classmethod
    def _make_client(cls, endpoint: str):
        """Build an S3 client for ``endpoint`` (empty -> real AWS from region).

        A custom endpoint (MinIO or any non-AWS host) needs **path-style**
        addressing — the bucket goes in the path, not as a subdomain of the
        host, which wouldn't resolve for ``minio:9000`` / an IP. Real AWS uses
        the default virtual-hosted style. SigV4 everywhere for presigned-url
        compatibility.

        The keys are passed only when both are set. Leaving them empty hands
        boto3 no explicit credentials, so it walks its own chain — environment,
        shared config, container role, instance role. That is what lets a
        deployment on AWS authenticate as the role its host already carries
        instead of storing a long-lived IAM user's key. Passing an empty string
        would not do this: botocore treats it as a credential rather than an
        absence, and every request fails to sign.
        """
        credentials = (
            {
                "aws_access_key_id": cls.access_key,
                "aws_secret_access_key": cls.secret_key,
            }
            if cls.access_key and cls.secret_key
            else {}
        )
        return boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            **credentials,
            region_name=cls.region,
            verify=cls.verify,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if endpoint else "auto"},
            ),
        )

    @classmethod
    def connect(cls) -> None:
        """Construct the client singletons (lazy: no socket until first call)."""
        cls.client = cls._make_client(cls.url)
        cls.public_client = cls._make_client(cls.public_url or cls.url)
        cls._bucket_ready = False

    @classmethod
    def _ensure_bucket(cls) -> None:
        """Create the results bucket if it doesn't exist (once per process)."""
        if cls._bucket_ready:
            return
        try:
            cls.client.head_bucket(Bucket=cls.bucket)
        except ClientError:
            # us-east-1 must omit LocationConstraint; other regions require it.
            if cls.region and cls.region != "us-east-1":
                cls.client.create_bucket(
                    Bucket=cls.bucket,
                    CreateBucketConfiguration={"LocationConstraint": cls.region},
                )
            else:
                cls.client.create_bucket(Bucket=cls.bucket)
        cls._bucket_ready = True

    @classmethod
    def connected(cls) -> bool:
        # list_buckets is a plain reachability check (independent of whether the
        # results bucket exists yet).
        try:
            cls.client.list_buckets()
            return True
        except Exception:
            return False

    @classmethod
    def put(
        cls, key: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> None:
        """Upload ``data`` under ``key`` (creating the bucket on first use)."""
        cls._ensure_bucket()
        cls.client.put_object(
            Bucket=cls.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    @classmethod
    def get(cls, key: str) -> Optional[bytes]:
        """Read the object at ``key``, or ``None`` if it doesn't exist.

        Used to read back small JSON blobs the server stores directly (e.g. a
        non-blocking request's latest status response), as opposed to the large
        result blobs the client downloads via a presigned url.

        Does not ensure the bucket (that's a write-path concern, and doing so here
        races): a missing key or bucket just means "nothing yet" -> None.
        """
        try:
            return cls.client.get_object(Bucket=cls.bucket, Key=key)["Body"].read()
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in (
                "NoSuchKey",
                "NoSuchBucket",
                "404",
            ):
                return None
            raise

    @classmethod
    def presigned_get(
        cls, key: str, expires: timedelta = timedelta(hours=1)
    ) -> str:
        """A presigned GET url for ``key`` the client can download directly."""
        return cls.public_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": cls.bucket, "Key": key},
            ExpiresIn=int(expires.total_seconds()),
        )


ObjectStoreProvider.from_env()
ObjectStoreProvider.connect()
