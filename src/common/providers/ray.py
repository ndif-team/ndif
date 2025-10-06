import logging
import os
import ray
from ray import serve

from . import Provider
from .util import verify_connection

logger = logging.getLogger("ndif")


class RayProvider(Provider):

    ray_url: str

    @classmethod
    def from_env(cls) -> None:
        super().from_env()
        cls.ray_url = os.environ.get("RAY_ADDRESS")

    @classmethod
    def to_env(cls) -> dict:
        return {
            **super().to_env(),
            "RAY_ADDRESS": cls.ray_url,
        }

    @classmethod
    def get_host_port(cls):
        """
        Returns a tuple (host, port) parsed from the ray_url.
        If port is not specified, defaults to 6379.
        """
        if not hasattr(cls, "ray_url") or not cls.ray_url:
            raise ValueError("ray_url is not set on RayProvider")
        if "://" in cls.ray_url:
            _, addr = cls.ray_url.split("://", 1)
        else:
            addr = cls.ray_url
        if "/" in addr:
            addr = addr.split("/", 1)[0]
        if ":" in addr:
            host, port = addr.split(":")
            port = int(port)
        else:
            logger.warning(f"RAY_ADDRESS ({cls.ray_url}) does not specify a port, using default port 6379")
            host = addr
            port = 6379  # Default Ray port if not specified
        return host, port

    @classmethod
    def is_listening(cls) -> bool:
        """Check if the Ray address is listening."""
        try:
            host, port = cls.get_host_port()
            return verify_connection(host, port)
        except Exception as e:
            return False

    @classmethod
    def connect(cls):
        host, port = cls.get_host_port()
        if not verify_connection(host, port):
            raise ConnectionError(f"Ray is not listening on {host}:{port}")

        logger.info(f"Connecting to Ray at {cls.ray_url}...")
        ray.init(logging_level="error", address=cls.ray_url)
        logger.info("Connected to Ray")

    @classmethod
    def connected(cls) -> bool:
        return ray.is_initialized() and cls.is_listening()

    @classmethod
    def reset(cls):
        ray.shutdown()
        serve.context._set_global_client(None)


RayProvider.from_env()
