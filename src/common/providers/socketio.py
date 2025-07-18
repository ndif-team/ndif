import logging
import os
import time

import socketio

from . import Provider, retry

logger = logging.getLogger("ndif")


class SioProvider(Provider):

    api_url: str
    sio: socketio.SimpleClient = None

    @classmethod
    def from_env(cls) -> None:
        super().from_env()
        cls.api_url = os.environ.get("API_URL")

    @classmethod
    def to_env(cls) -> dict:
        return {
            **super().to_env(),
            "API_URL": cls.api_url,
        }

    @classmethod
    @retry
    def connect(cls):
        logger.info(f"Connecting to API at {cls.api_url}...")
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
        logger.info("Connected to API")

    @classmethod
    def disconnect(cls):
        logger.debug("SioProvider.disconnect() called")
        if cls.sio is not None:
            logger.info("Disconnecting socketio client")
            cls.sio.disconnect()
            logger.debug("Socketio client disconnected")
        else:
            logger.debug("No socketio client to disconnect")

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


SioProvider.from_env()
