import logging
from functools import lru_cache
from typing import Any, Dict, List
import os

from ray import serve
from ndif_common.providers.ray import RayProvider
from ndif_common.schema import BackendRequestModel
from ndif_common.types import MODEL_KEY

from .base import Coordinator
from ..processing.request_processor import RequestProcessor
from ..processing.status import DeploymentStatus, ProcessorStatus
from ..util import cache_maintainer

import time

logger = logging.getLogger("ndif")

DEPLOYMENT_TIMEOUT_SECONDS = float(os.environ.get("DEPLOYMENT_TIMEOUT_SECONDS", 10))


class RequestCoordinator(Coordinator[BackendRequestModel, RequestProcessor]):
    """
    Coordinates requests between the queue and the model deployments using Ray backend.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        RayProvider.watch()

        self._controller = None

    @property
    def backend_handle(self):
        """Get the Ray controller handle for deployment operations."""

        if not self.connected:
            self._controller = None

        elif self._controller is None:
            start = time.perf_counter()
            self._controller = serve.get_app_handle("Controller")
            logger.debug(
                f"Created controller app handle in {time.perf_counter() - start} seconds"
            )
        return self._controller

    @property
    def connected(self) -> bool:
        """Check if the coordinator is connected to the Ray controller."""
        try:
            self._connected = RayProvider.connected()
        except Exception as e:
            logger.exception(f"Error checking Ray connection: {e}")
            self._connected = False
        if not self._connected:
            self._controller = None
        return self._connected

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the coordinator."""
        state = super().get_state()
        state["ray_connected"] = self.connected
        return state

    def route_request(self, request: BackendRequestModel) -> None:
        """Route a request to the coordinator."""
        if not self.connected:
            raise RuntimeError("Ray controller is not connected.")

        # For now, only dedicated models will be accessible to users not in the hotswapping pilot program.
        if not request.hotswapping:
            if not self._is_dedicated(request.model_key):
                raise RuntimeError(
                    f"Model {request.model_key} is not dedicated and hotswapping is not supported for this API key."
                )

        return super().route_request(request)

    def _get_processor_key(self, request: BackendRequestModel) -> str:
        """Extract the model key from a backend request."""
        return request.model_key

    def _get_failure_message(self, processor: RequestProcessor, status) -> str:
        """
        Generate Ray-specific failure messages based on processor status.

        Args:
            processor: The failed RequestProcessor
            status: The processor's current status

        Returns:
            A user-facing message explaining why the request processor failed
        """
        if status == ProcessorStatus.TERMINATED:
            return (
                f"Deployment for {processor.id} has been terminated by the scheduler. "
                "You can request it to be rescheduled by re-running your nnsight script."
            )
        elif status == ProcessorStatus.UNAVAILABLE:
            task_status = processor.backend_status
            if task_status == DeploymentStatus.CANT_ACCOMMODATE:
                return (
                    f"Your request could not be processed because {processor.id} is currently unavailable for deployment, "
                    "either due to size constraints or loading issues. "
                    "If you believe this model should be supported, feel free to make a post on https://discuss.ndif.us/ "
                    "or raise a Github issue: https://github.com/ndif-team/ndif/issues"
                )
            else:
                logger.exception(
                    f"Processor for {processor.id} was set to UNAVAILABLE, but the deployment status is: {task_status}. This is unexpected."
                )
                return "Your request could not be processed because the model deployment became unavailable due to an internal error. Please try again in a few moments."
        else:
            logger.exception(f"Unknown processor failure: {status}")
            return "Your request could not be processed because the model deployment encountered an unexpected error. Please try again in a few moments."

    def _deploy(self, processors: List[RequestProcessor]):
        """
        Attempts to deploy a list of RequestProcessors by invoking the controller's deploy method.
        Updates each processor's deployment status based on the controller's response.

        Args:
            processors (List[RequestProcessor]): The processors to deploy.
        """
        if not processors:
            return

        try:
            # Prepare model keys for deployment
            model_keys = [processor.id for processor in processors]

            self._require_connected()
            # Initiate deployment via Ray controller (Ray 2.47.0)
            deployment_future = self.backend_handle.deploy.remote(model_keys)

            self._require_connected()
            # Synchronously fetch deployment results (blocking)
            deployment_results = deployment_future._fetch_future_result_sync()

            self._require_connected()
            # The .get() is a hack to retrieve the result with a timeout
            deployment_statuses, evictions = deployment_results.get(
                DEPLOYMENT_TIMEOUT_SECONDS
            ).values()

            logger.debug(f"Deployment results from controller: {deployment_statuses}")

            for processor in processors:
                # Retrieve and normalize the deployment status for this processor
                status_str = str(deployment_statuses[processor.id]).lower()
                try:
                    deployment_status = DeploymentStatus(status_str)
                    logger.info(
                        f"[COORDINATOR] Processor {processor.id} deployment status: {deployment_status}"
                    )
                except ValueError:
                    logger.exception(
                        f"Unknown processor status '{status_str}' for model_key '{processor.id}'"
                    )
                    deployment_status = DeploymentStatus.CANT_ACCOMMODATE

                # Store the processor's deployment status in a clear attribute
                processor.backend_status = deployment_status

            if evictions:
                logger.info(
                    f"[COORDINATOR] Controller evicted {len(evictions)} processors: {evictions}"
                )
                user_message = (
                    "Your request could not be processed because the model was unloaded to make room for another model. "
                    "Please submit your request again to attempt redeployment."
                )
                for model_key in evictions:
                    try:
                        self.evict_processor(model_key, user_message=user_message)
                        logger.debug(f"Evicted {model_key}")
                    except Exception as e:
                        logger.exception(f"Failed to evict {model_key}: {e}")
            else:
                logger.debug("[COORDINATOR] No processors were evicted by controller")

        except Exception as e:
            logger.exception(f"Error during processor deployment: {e}")

    def _create_processor(self, processor_key: str) -> RequestProcessor:
        """Create a new RequestProcessor."""
        return RequestProcessor(processor_key, max_retries=self.max_retries)

    def _process_lifecycle_tick(self):
        """Before processing lifecycle tick, see if service is disconnected from Controller, if it is, fail the processors"""

        # MR Sept 10: I'm not super satisfied with this pattern, but it does prevent "zombie processors" from occuring and immediately notifies queued users when the service is disconnected from the backend.
        if not self.connected:
            for processor in self.active_processors.values():
                self._handle_processor_failure(processor)
        return super()._process_lifecycle_tick()

    def _get_eviction_message(self, context: str) -> str:
        """
        Get request-specific eviction messages.

        Args:
            context: Either "execution" or "queue"

        Returns:
            A status message describing what happened to the request
        """
        if context == "execution":
            return "Request interrupted during execution."
        elif context == "queue":
            return "Request removed from queue."
        else:
            return "Request interrupted."

    # Helper to ensure Ray is connected before each operation
    def _require_connected(self):
        """Helper which raises an error if not connected to Ray Serve."""
        if not self.connected:
            raise RuntimeError("Ray Serve is not connected.")

    @cache_maintainer(clear_time=600)
    @lru_cache(maxsize=1000)
    def _is_dedicated(self, model_key: MODEL_KEY):
        """Helper which returns True if the model is dedicated."""
        try:
            self._require_connected()
            future = self.backend_handle.get_deployment.remote(model_key)
            self._require_connected()
            results = future._fetch_future_result_sync()
            deployment_dict = results.get(DEPLOYMENT_TIMEOUT_SECONDS)
            if deployment_dict:
                return deployment_dict.get("dedicated", False)
            else:
                return False
        except Exception as e:
            logger.exception(f"Error checking if model {model_key} is dedicated: {e}")
            return False
