"""A pickled COMPLETED response reaches the websocket through a Redis key.

``respond(pickled=True)`` stores the envelope under ``result:{id}`` and
publishes only a short notice; ``/subscribe`` GETDELs the key and sends the
bytes as a binary frame. Needs a reachable Redis (``NDIF_REDIS_URL``); skips
without one.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from nnsight.schema.response import ResponseModel

from ndif.common.providers.redis import RedisProvider
from ndif.common.schema import BackendRequestModel, Status

pytestmark = pytest.mark.skipif(not RedisProvider.connected(), reason="no redis")


def test_result_blob_arrives_as_binary_frame_and_key_is_consumed():
    from ndif.services.api.app import app

    blob = os.urandom(3 << 20)
    with TestClient(app) as client, client.websocket_connect("/subscribe") as ws:
        session_id = ws.receive_json()["session_id"]
        request = BackendRequestModel(model_key="m", session_id=session_id)
        tap = RedisProvider.sync_client.pubsub()
        tap.subscribe(session_id)
        tap.get_message(timeout=1)

        request.respond(Status.COMPLETED, data=blob, pickled=True)
        back = ResponseModel.unpickle(ws.receive_bytes())
        notice = tap.get_message(timeout=1)
        tap.close()

    assert len(notice["data"]) < 100

    assert back.id == request.id
    assert back.status == Status.COMPLETED
    assert back.data == blob
    assert RedisProvider.sync_client.exists(f"result:{request.id}") == 0


def test_result_without_subscriber_leaves_no_key():
    request = BackendRequestModel(model_key="m", session_id="nobody-listens")

    request.respond(Status.COMPLETED, data=b"x", pickled=True)

    assert RedisProvider.sync_client.exists(f"result:{request.id}") == 0
