import asyncio
import logging
import os
import time

import socketio

from . import Provider, retry

logger = logging.getLogger("ndif")


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
        if cls.sio is not None:
            cls.sio.connected = False

    @classmethod
    @retry
    def call(cls, *args, **kwargs):
        return cls.sio.client.call(*args, **kwargs)

    @classmethod
    @retry
    def emit(cls, *args, **kwargs):
        return cls.sio.client.emit(*args, **kwargs)

    # ── Async methods (used by Dispatcher) ──

    @classmethod
    def async_connected(cls) -> bool:
        return cls._async_sio is not None and cls._async_sio.connected

    @classmethod
    async def async_connect(cls):
        if cls._async_connect_lock is None:
            cls._async_connect_lock = asyncio.Lock()

        async with cls._async_connect_lock:
            if cls.async_connected():
                return

            for attempt in range(cls.max_retries):
                try:
                    cls._async_sio = socketio.AsyncSimpleClient(
                        reconnection_attempts=10
                    )
                    await cls._async_sio.connect(
                        cls.api_url,
                        socketio_path="/ws/socket.io",
                        transports=["websocket"],
                        wait_timeout=100000,
                    )
                    return
                except Exception as e:
                    if attempt == cls.max_retries - 1:
                        raise
                    logger.warning(
                        f"Async SIO connect attempt {attempt + 1} failed: {e}"
                    )
                    await asyncio.sleep(cls.retry_interval)

    @classmethod
    async def async_call(cls, *args, **kwargs):
        if not cls.async_connected():
            await cls.async_connect()
        return await cls._async_sio.call(*args, **kwargs)

    @classmethod
    async def async_emit(cls, *args, **kwargs):
        if not cls.async_connected():
            await cls.async_connect()
        return await cls._async_sio.emit(*args, **kwargs)


SioProvider.from_env()
