"""Queue coordinator for routing API requests to model processors.

The coordinator:
- Receives serialized `BackendRequestModel` items from a Redis list ("queue")
- Routes each request to the `Processor` for its `model_key`
- Requests deployments via the Ray Serve `Controller` when processors are missing
- Tracks deployment futures and advances processors through lifecycle states
- Serves status snapshots to consumers via a Redis list ("status")
"""

import json
import os
import pickle
import time
from concurrent.futures import Future
from enum import Enum
from functools import lru_cache
from typing import Optional

import redis
from ray import serve

from ..logging import set_logger
from ..providers.ray import RayProvider
from ..schema import BackendRequestModel, BackendResponseModel
from .processor import Processor, ProcessorStatus
from .util import patch, cache_maintainer


class DeploymentStatus(Enum):
    """Deployment states reported by the controller."""

    DEPLOYED = "deployed"
    CACHED_AND_FREE = "cached_and_free"
    FREE = "free"
    CACHED_AND_FULL = "cached_and_full"
    FULL = "full"
    CANT_ACCOMMODATE = "cant_accommodate"


class Coordinator:
    """Orchestrates request routing and model deployment lifecycle."""

    def __init__(self):

        self.redis_client = redis.Redis.from_url(os.environ.get("BROKER_URL"))
        self.processors: dict[str, Processor] = {}

        self.deployment_futures: list[Future] = []
        self.processors_to_deploy: list[Processor] = []

        self.status_future: Future = None
        self.status_cache = None
        self.last_status_time = 0
        self.status_cache_freq_s = int(os.environ.get("COORDINATOR_STATUS_CACHE_FREQ_S", "120"))

        self.logger = set_logger("coordinator")

        # We patch the _async_send method to avoid a nasty deadlock bug in Ray.
        patch()

        # Connect to Ray initially.
        self.connect()

    @classmethod
    def start(cls):
        """Start a coordinator and enter its main loop."""
        coordinator = cls()
        coordinator.loop()

    @property
    def controller_handle(self):
        """Return Ray Serve handle to the `Controller` application."""
        return serve.get_app_handle("Controller")

    def connect(self):
        """Ensure connection to Ray, retrying until successful.

        On errors, purge processors to reset local state before reconnecting.
        """
        self.logger.info(f"Connecting to Ray")

        while not RayProvider.connected():

            self.purge()

            try:

                RayProvider.reset()
                RayProvider.connect()

            except Exception as e:
                self.logger.error(f"Error connecting to Ray: {e}")

    def loop(self):
        """Main event loop for routing, deploying, and stepping processors."""
        while True:

            try:
                # Get all requests currently in the queue and route them to the appropriate processors.
                for _ in range(self.redis_client.llen("queue")):
                    request = self.get()
                    self.route(request)

                # If there are processors waiting to be deployed, deploy them.
                if len(self.processors_to_deploy) > 0:
                    self.deploy()

                # If there are deployments in progress, check their status.
                if len(self.deployment_futures) > 0:
                    self.initialize()

                # Step each processor to advance its state machine.
                for processor in self.processors.values():
                    processor.step()

                # Serve controller status snapshots to waiting Redis consumers.
                self.fulfill_status()

            # If there is an error in the coordinator loop, it might be due to a connection issue.
            # So we reconnect to Ray and try again.
            except Exception as e:
                self.logger.error(f"Error in coordinator loop: {e}")
                self.connect()

    def deploy(self):
        """Batch request deployments for processors awaiting provisioning."""

        handle = self.controller_handle

        model_keys = []

        for processor in self.processors_to_deploy:

            model_keys.append(processor.model_key)
            processor.status = ProcessorStatus.PROVISIONING

        self.deployment_futures.append(handle.deploy.remote(model_keys))
        self.processors_to_deploy.clear()

    def get(self):
        """Pop one serialized request from Redis and deserialize it."""
        return pickle.loads(self.redis_client.brpop("queue")[1])

    def route(self, request: BackendRequestModel):
        """Route a request to the per-model processor, creating it if missing."""

        # If user does not have hotswapping access, check that the model is dedicated.
        if not request.hotswapping:
            if not self.is_dedicated_model(request.model_key):
                request.create_response(
                    status=BackendResponseModel.JobStatus.ERROR,
                    description=f"Model {request.model_key} is not a scheduled model and hotswapping is not supported for this API key. See https://nnsight.net/status/ for a list of scheduled models.",
                    logger=self.logger,
                ).respond()
                return
       
        if request.model_key not in self.processors:

            self.processors[request.model_key] = Processor(request.model_key)
            self.processors_to_deploy.append(self.processors[request.model_key])

        self.processors[request.model_key].enqueue(request)

    def initialize(self):
        """Advance deployment futures and update processor states."""

        ready = []
        not_ready = []

        for deployment_future in self.deployment_futures:

            try:

                result = deployment_future.result(timeout_s=0)

            except TimeoutError:
                not_ready.append(deployment_future)

            else:
                ready.append(result)

        for result in ready:

            deployment_statuses = result["result"]

            evictions = result["evictions"]

            for model_key, status in deployment_statuses.items():

                status_str = str(status).lower()

                try:

                    deployment_status = DeploymentStatus(status_str)

                except ValueError:

                    self.remove(
                        model_key,
                        message=f"{status_str}\n\nThere was an error provisioning the model deployment. Sorry for the inconvenience.",
                    )

                    continue

                if deployment_status == DeploymentStatus.CANT_ACCOMMODATE:

                    self.remove(
                        model_key,
                        message="Model deployment cannot be accomodated at this time. Please try again later. Sorry for the inconvenience.",
                    )

                    continue

                else:

                    self.processors[model_key].status = ProcessorStatus.DEPLOYING

            for eviction in evictions:
                self.remove(
                    eviction,
                    message="Model deployment evicted. Please try again later. Sorry for the inconvenience.",
                )

        self.deployment_futures = not_ready

    def purge(self):
        """Remove all processors and purge their pending work."""
        for model_key in list(self.processors.keys()):
            self.remove(model_key)

    def remove(self, model_key: str, message: Optional[str] = None):
        """Remove a processor and purge its outstanding work.

        Args:
            model_key: Model identifier for the processor to remove.
            message: Optional message sent to any queued requests.
        """
        processor = self.processors.pop(model_key)

        processor.purge(message=message)

    def fulfill_status(self):
        """Serve controller status snapshots to waiting Redis consumers."""
        if self.status_future is not None:

            try:

                result = self.status_future.result(timeout_s=0)

            except TimeoutError:
                return

            else:

                status = json.dumps(result)

                for _ in range(self.redis_client.llen("status")):
                    id = self.redis_client.brpop("status")[1]
                    self.redis_client.lpush(id, status)

                self.status_future = None
                self.last_status_time = time.time()
                self.status_cache = status

        elif self.redis_client.llen("status") > 0:

            if (
                self.status_cache is None
                or time.time() - self.last_status_time > self.status_cache_freq_s
            ):

                self.status_future = self.controller_handle.status.remote()

            else:

                for _ in range(self.redis_client.llen("status")):
                    id = self.redis_client.brpop("status")[1]
                    self.redis_client.lpush(id, status)

    @cache_maintainer(clear_time=600)
    @lru_cache(maxsize=1000)
    def is_dedicated_model(self, model_key: str) -> bool:
        """Check if the model is dedicated."""
        try:
            result = self.controller_handle.get_deployment.remote(model_key).result(
                timeout_s=int(os.environ.get("COORDINATOR_HANDLE_TIMEOUT_S", "5"))
            )

            # Dedicated models are deployed automatically on startup - the absence of a deployment means it's not dedicated.
            if result is None:
                return False

            return result.get("dedicated", False)

        except Exception as e:
            self.logger.error(f"Error checking if model is dedicated: {e}")
            return False