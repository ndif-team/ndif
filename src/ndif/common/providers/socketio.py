import asyncio
import logging
import os
import time

import socketio
from socketio.exceptions import (
    BadNamespaceError,
    ConnectionError as SioConnectionError,
    DisconnectedError,
)

from . import Provider, retry, async_retry

logger = logging.getLogger("ndif")

# socket.io errors that mean the connection itself is gone (as opposed to
# TimeoutError, which fires on a *healthy* connection when an ack is just slow).
# When we hit one of these the client must be torn down and rebuilt; a plain
# retry on the same dead client would loop forever.
CONNECTION_BROKEN_ERRORS = (BadNamespaceError, DisconnectedError, SioConnectionError)


class SioProvider(Provider):
    api_url: str
    sio: socketio.SimpleClient = None
    _async_sio: socketio.AsyncSimpleClient = None
    _async_connect_lock: asyncio.Lock = None

    @classmethod
    def from_env(cls) -> None:
        super().from_env()
        cls.api_url = os.environ.get("NDIF_API_URL")

    @classmethod
    def to_env(cls) -> dict:
        return {
            **super().to_env(),
            "NDIF_API_URL": cls.api_url,
        }

    # ── Sync methods (used by Ray service) ──

    @classmethod
    @retry
    def connect(cls):
        if cls.sio is None:
            logger.debug("Creating new socketio client")
            cls.sio = socketio.SimpleClient(reconnection_attempts=10)

        cls.sio.connect(
            f"{cls.api_url}",
            socketio_path="/ws/socket.io",
            transports=["websocket"],
            wait_timeout=100000,
        )
        # Wait for connection to be fully established
        time.sleep(0.1)

    @classmethod
    def disconnect(cls):
        if cls.sio is not None:
            cls.sio.disconnect()

    @classmethod
    def connected(cls) -> bool:
        return cls.sio is not None and cls.sio.client is not None and cls.sio.connected

    @classmethod
    def reset(cls):
        # Force the underlying client to disconnect (stopping its read/write
        # loops and any background reconnect) and drop the wrapper entirely so
        # the next connect() rebuilds a fully fresh client. Flipping `connected`
        # alone would leave a wedged client (and its leaked engineio threads) in
        # place, which is what keeps the actor permanently unable to deliver.
        if cls.sio is not None:
            try:
                if cls.sio.client is not None:
                    cls.sio.client.disconnect()
            except Exception:
                pass
            cls.sio = None

    @classmethod
    @retry
    def call(cls, *args, **kwargs):
        try:
            return cls.sio.client.call(*args, **kwargs)
        except CONNECTION_BROKEN_ERRORS:
            # Connection is gone — flag it dead so retry's proactive path
            # (`if not connected(): reset(); connect()`) tears it down and
            # rebuilds on the next attempt, rather than reusing the dead client.
            cls.sio.connected = False
            raise

    @classmethod
    @retry
    def emit(cls, *args, **kwargs):
        try:
            return cls.sio.client.emit(*args, **kwargs)
        except CONNECTION_BROKEN_ERRORS:
            cls.sio.connected = False
            raise

    # ── Async methods (used by Dispatcher) ──

    @classmethod
    def async_connected(cls) -> bool:
        return (
            cls._async_sio is not None
            and cls._async_sio.client is not None
            and cls._async_sio.connected
        )

    @classmethod
    @async_retry
    async def async_connect(cls):
        if cls._async_connect_lock is None:
            cls._async_connect_lock = asyncio.Lock()

        # Lock so concurrent Dispatcher tasks don't race to rebuild the client;
        # the double-check inside avoids a redundant reconnect once we hold it.
        async with cls._async_connect_lock:
            if cls.async_connected():
                return

            # Own the full connection lifecycle here, under the lock: drop any
            # existing client, build a fresh one, and connect. Keeping reset and
            # connect together under the lock means one task rebuilds the
            # connection while concurrent callers wait and then reuse it.
            await cls.async_reset()

            logger.debug("Creating new async socketio client")
            cls._async_sio = socketio.AsyncSimpleClient(reconnection_attempts=10)

            await cls._async_sio.connect(
                cls.api_url,
                socketio_path="/ws/socket.io",
                transports=["websocket"],
                wait_timeout=100000,
            )

    @classmethod
    async def async_reset(cls):
        # Async counterpart of reset(): disconnect the underlying client and drop
        # the wrapper so the next async_connect() rebuilds a fully fresh one
        # (rather than leaking the old client's tasks behind a flipped flag).
        if cls._async_sio is not None:
            try:
                if cls._async_sio.client is not None:
                    await cls._async_sio.client.disconnect()
            except Exception:
                pass
            cls._async_sio = None

    @classmethod
    @async_retry
    async def async_call(cls, *args, **kwargs):
        try:
            return await cls._async_sio.call(*args, **kwargs)
        except CONNECTION_BROKEN_ERRORS:
            # Connection is gone — flag it dead so async_retry's proactive path
            # tears it down and rebuilds on the next attempt.
            cls._async_sio.connected = False
            raise

    @classmethod
    @async_retry
    async def async_emit(cls, *args, **kwargs):
        try:
            return await cls._async_sio.emit(*args, **kwargs)
        except CONNECTION_BROKEN_ERRORS:
            cls._async_sio.connected = False
            raise


SioProvider.from_env()
