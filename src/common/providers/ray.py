import logging
import os

import ray
from ray import serve

from . import Provider

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
    def connect(cls):
        logger.info(f"Connecting to Ray at {cls.ray_url}...")
        ray.init(logging_level="error", address=cls.ray_url)
        logger.info("Connected to Ray")

    @classmethod
    def connected(cls) -> bool:
        return ray.is_initialized()

    @classmethod
    def reset(cls):
        ray.shutdown()
        serve.context._set_global_client(None)


RayProvider.from_env()
