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

from . import Provider

logger = logging.getLogger("ndif")

# Errors that mean the connection is down or corrupted: the client must be torn
# down and rebuilt before the next attempt. A slow ack raises TimeoutError
# instead — that's a *healthy* connection, so we retry the call without
# rebuilding.
CONNECTION_BROKEN_ERRORS = (BadNamespaceError, DisconnectedError, SioConnectionError)

# Bound a single connect attempt. engineio passes no connect-level timeout to
# aiohttp, so without this a cold connect can block on DNS/TCP indefinitely.
CONNECT_TIMEOUT_S = float(os.environ.get("NDIF_SIO_CONNECT_TIMEOUT_S", 30))

CONNECT_KWARGS = dict(
    socketio_path="/ws/socket.io",
    transports=["websocket"],
    wait_timeout=100000,
)


class SioProvider(Provider):
    """A self-healing socket.io client.

    ``emit``/``call`` (and their ``async_`` twins) run the op against the
    underlying client; if the connection is down or corrupted they rebuild it
    and retry. The sync and async halves are deliberate mirrors of each other —
    keep them that way.
    """

    api_url: str
    sio: socketio.SimpleClient = None
    _async_sio: socketio.AsyncSimpleClient = None
    _async_lock: asyncio.Lock = None

    @classmethod
    def from_env(cls) -> None:
        super().from_env()
        cls.api_url = os.environ.get("NDIF_API_URL")
        # Build the lock eagerly so concurrent tasks never race to create it.
        # Constructing it outside a running loop is fine on 3.10+ — it binds to
        # the loop on first acquire (the dispatcher's loop, inside this process).
        cls._async_lock = asyncio.Lock()

    @classmethod
    def to_env(cls) -> dict:
        return {**super().to_env(), "NDIF_API_URL": cls.api_url}

    # ── Sync (Ray service) ──

    @classmethod
    def connected(cls) -> bool:
        return cls.sio is not None and cls.sio.client is not None and cls.sio.connected

    @classmethod
    def reset(cls) -> None:
        # Disconnect the underlying client (stopping its read/write loops and any
        # background reconnect) and drop the wrapper so the next connect rebuilds
        # fresh. Leaving a corrupted client in place is what wedges delivery.
        if cls.sio is not None:
            try:
                if cls.sio.client is not None:
                    cls.sio.client.disconnect()
            except Exception:
                pass
            cls.sio = None

    @classmethod
    def connect(cls) -> None:
        if cls.connected():
            return
        cls.reset()
        logger.debug("Creating new socketio client")
        cls.sio = socketio.SimpleClient(reconnection_attempts=10)
        cls.sio.connect(cls.api_url, **CONNECT_KWARGS)

    @classmethod
    def disconnect(cls) -> None:
        cls.reset()

    @classmethod
    def emit(cls, *args, **kwargs):
        return cls._run("emit", *args, **kwargs)

    @classmethod
    def call(cls, *args, **kwargs):
        return cls._run("call", *args, **kwargs)

    @classmethod
    def _run(cls, op: str, *args, **kwargs):
        error = None
        for attempt in range(cls.max_retries):
            try:
                if not cls.connected():
                    cls.connect()
                return getattr(cls.sio.client, op)(*args, **kwargs)
            except CONNECTION_BROKEN_ERRORS as e:
                error = e
                cls.reset()  # corrupted — rebuild on the next attempt
            except Exception as e:
                error = e  # transient (e.g. slow ack) — retry on the same client
            logger.debug(
                "SioProvider.%s attempt %d/%d failed: %s",
                op, attempt + 1, cls.max_retries, error,
            )
            time.sleep(cls.retry_interval)
        raise error

    # ── Async (Dispatcher) ──

    @classmethod
    def async_connected(cls) -> bool:
        return (
            cls._async_sio is not None
            and cls._async_sio.client is not None
            and cls._async_sio.connected
        )

    @classmethod
    async def _teardown(cls) -> None:
        # Mirror of reset(). Caller must hold _async_lock.
        if cls._async_sio is not None:
            try:
                if cls._async_sio.client is not None:
                    await cls._async_sio.client.disconnect()
            except Exception:
                pass
            cls._async_sio = None

    @classmethod
    async def async_reset(cls) -> None:
        async with cls._async_lock:
            await cls._teardown()

    @classmethod
    async def async_connect(cls) -> None:
        # The whole tear-down + rebuild runs under the lock, so a concurrent task
        # can never disconnect a client this one is mid-connect on. That race
        # injected a CancelledError into aiohttp's DNS lookup and, since
        # CancelledError is a BaseException, it dodged every retry and crashed
        # the dispatcher.
        async with cls._async_lock:
            if cls.async_connected():
                return
            await cls._teardown()
            logger.debug("Creating new async socketio client")
            cls._async_sio = socketio.AsyncSimpleClient(reconnection_attempts=10)
            try:
                await asyncio.wait_for(
                    cls._async_sio.connect(cls.api_url, **CONNECT_KWARGS),
                    timeout=CONNECT_TIMEOUT_S,
                )
            except BaseException:
                # Discard the half-built client. A hung connect becomes a
                # retryable TimeoutError; a genuine CancelledError (shutdown)
                # still propagates rather than being swallowed.
                await cls._teardown()
                raise

    @classmethod
    async def async_emit(cls, *args, **kwargs):
        return await cls._arun("emit", *args, **kwargs)

    @classmethod
    async def async_call(cls, *args, **kwargs):
        return await cls._arun("call", *args, **kwargs)

    @classmethod
    async def _arun(cls, op: str, *args, **kwargs):
        error = None
        for attempt in range(cls.max_retries):
            try:
                if not cls.async_connected():
                    await cls.async_connect()
                return await getattr(cls._async_sio.client, op)(*args, **kwargs)
            except CONNECTION_BROKEN_ERRORS as e:
                error = e
                await cls.async_reset()  # corrupted — rebuild on the next attempt
            except Exception as e:
                error = e  # transient (e.g. slow ack) — retry on the same client
            logger.debug(
                "SioProvider.async_%s attempt %d/%d failed: %s",
                op, attempt + 1, cls.max_retries, error,
            )
            await asyncio.sleep(cls.retry_interval)
        raise error


SioProvider.from_env()
