"""Large result blobs upload as a parallel multipart upload and read back intact.

Needs a reachable object store (the dev MinIO by default, configured by the
same ``NDIF_OBJECT_STORE_*`` env the server reads); skips without one.
"""

from __future__ import annotations

import os

import pytest

from ndif.common.providers.objectstore import _TRANSFER, ObjectStoreProvider as OS

pytestmark = pytest.mark.skipif(not OS.connected(), reason="no object store")


@pytest.mark.parametrize(
    "size, parts",
    [
        (1000, None),
        (2 * _TRANSFER.multipart_chunksize + 1, 3),
    ],
)
def test_put_round_trips(size, parts):
    key = f"test-put-{size}"
    data = os.urandom(size)
    OS.put(key, data, content_type="application/x-test")
    try:
        assert OS.get(key) == data
        head = OS.client.head_object(Bucket=OS.bucket, Key=key)
        assert head["ContentType"] == "application/x-test"
        # A multipart object's ETag is "<md5 of part md5s>-<part count>".
        etag = head["ETag"].strip('"')
        assert etag.endswith(f"-{parts}") if parts else "-" not in etag
    finally:
        OS.client.delete_object(Bucket=OS.bucket, Key=key)
