import logging
from typing import Any, Dict, List

from ray import serve

from ..processing.request_processor import RequestProcessor
from ..processing.status import DeploymentStatus, ProcessorStatus
from ..providers.ray import RayProvider
from ..schema import BackendRequestModel
from .base import Coordinator

import time

logger = logging.getLogger("ndif")

# Constants
DEPLOYMENT_TIMEOUT_SECONDS = 3.14  # Timeout for deployment results

class RequestCoordinator(Coordinator[BackendRequestModel, RequestProcessor]):
    """
    Coordinates requests between the queue and the model deployments using Ray backend.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        RayProvider.watch()

        self._controller = None

    @property
    def controller(self):
        """Creates a Controller handle used to submit requests for models to deploy"""
        if self._controller is None:
            start = time.perf_counter()
            self._controller = serve.get_app_handle("Controller")
            logger.debug(f"Created controller app handle in {time.perf_counter() - start} seconds")
        return self._controller

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the coordinator. Adds ray_connected to the base state."""
        base_state = super().get_state()
        base_state["ray_connected"] = RayProvider.connected
        return base_state

    def route_request(self, request: BackendRequestModel) -> bool:
        """Route request to appropriate processor.

        Returns:
            True if request was routed successfully, False otherwise.
        """
        try:
            # Validate request
            if not request or not request.model_key:
                logger.exception("Invalid request: missing model_key")
                return False

            model_key = request.model_key

            # Try to route to existing active processor
            if model_key in self.active_processors:
                processor = self.active_processors[model_key]
                success = processor.enqueue(request)
                if success:
                    logger.debug(
                        f"Request {request.id} routed to active processor {model_key}"
                    )
                    return True
                else:
                    logger.exception(
                        f"Failed to enqueue request {request.id} to active processor {model_key}"
                    )
                    return False

            # Try to route to inactive processor
            elif model_key in self.inactive_processors:
                processor = self.inactive_processors[model_key]

                # Restart the processor to ensure clean state
                processor.restart()
                success = processor.enqueue(request)
                if success:
                    # Move processor to active state
                    self.active_processors[model_key] = processor
                    del self.inactive_processors[model_key]
                    logger.info(
                        f"Activated processor {model_key} and routed request {request.id}"
                    )
                    return True
                else:
                    logger.exception(
                        f"Failed to enqueue request {request.id} to inactive processor {model_key}"
                    )
                    return False

            # Create new processor
            else:
                try:
                    processor = self._create_processor(model_key)
                    success = processor.enqueue(request)
                    if success:
                        self.active_processors[model_key] = processor
                        logger.info(
                            f"Created new processor {model_key} and routed request {request.id}"
                        )
                        return True
                    else:
                        logger.exception(
                            f"Failed to enqueue request {request.id} to new processor {model_key}"
                        )
                        return False
                except Exception as e:
                    logger.exception(f"Failed to create processor {model_key}: {e}")
                    return False


        except Exception as e:
            logger.exception(
                f"Error routing request {request.id if request else 'unknown'}: {e}"
            )
            return False

        finally:
            # Force next tick to start immediately
            self._interrupt_sleep()

    def remove_request(self, request_id : str) -> bool:
        """Initiates process to have request deleted from queue."""
        for processor in self.active_processors.values():
            if processor.has_request(request_id):
                processor.deletion_queue.append(request_id)
                logger.debug(f"[COORDINATOR] Processor {processor.model_key} had request placed on deletion queue: {request_id}")
                return True

        logger.debug(f"[COORDINATOR] Request {request_id} attempted to be removed from queue but was not found in any active processors")
        return False

    def _handle_processor_failure(self, processor: RequestProcessor):
        """
        Handle a failed processor by notifying users, clearing its queue,
        and moving it from active to inactive processors.
        """
        processor_status = processor.status

        if processor_status == ProcessorStatus.TERMINATED:
            user_message = (
                f"Deployment for {processor.model_key} has been terminated by the scheduler. "
                "You can request it to be rescheduled by re-running your nnsight script."
            )
        elif processor_status == ProcessorStatus.UNAVAILABLE:
            task_status = processor.deployment_status
            if task_status == DeploymentStatus.CANT_ACCOMMODATE:
                user_message = (
                    f"Your request could not be processed because {processor.model_key} is currently unavailable for deployment, "
                    "either due to size constraints or loading issues. "
                    "If you believe this model should be supported, feel free to make a post on https://discuss.ndif.us/ "
                    "or raise a Github issue: https://github.com/ndif-team/ndif/issues"
                )
            else:
                logger.exception(
                    f"Processor for {processor.model_key} was set to UNAVAILABLE, but the deployment status is: {task_status}. This is unexpected."
                )
                user_message = "Your request could not be processed because the model deployment became unavailable due to an internal error. Please try again in a few moments."
        else:
            logger.exception(f"Unknown processor failure: {processor_status}")
            user_message = "Your request could not be processed because the model deployment encountered an unexpected error. Please try again in a few moments."

        # Notify all queued requests of the failure
        self.evict_processor(processor.model_key, user_message=user_message)

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
            model_keys = [processor.model_key for processor in processors]

            # Initiate deployment via Ray controller (Ray 2.47.0)
            deployment_future = self.controller.deploy.remote(model_keys)

            # Synchronously fetch deployment results (blocking)
            deployment_results = deployment_future._fetch_future_result_sync()
            # The .get() is a hack to retrieve the result with a timeout
            deployment_statuses, evictions = deployment_results.get(DEPLOYMENT_TIMEOUT_SECONDS).values()

            logger.debug(f"Deployment results from controller: {deployment_statuses}")

            for processor in processors:
                # Retrieve and normalize the deployment status for this processor
                status_str = str(deployment_statuses[processor.model_key]).lower()
                try:
                    deployment_status = DeploymentStatus(status_str)
                    logger.info(f"[COORDINATOR] Processor {processor.model_key} deployment status: {deployment_status}")
                except ValueError:
                    logger.exception(
                        f"Unknown processor status '{status_str}' for model_key '{processor.model_key}'"
                    )
                    deployment_status = DeploymentStatus.CANT_ACCOMMODATE

                # Store the processor's deployment status in a clear attribute
                processor.deployment_status = deployment_status

            if evictions:
                logger.info(f"[COORDINATOR] Controller evicted {len(evictions)} processors: {evictions}")
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
        return RequestProcessor(processor_key, self.max_retries)

    def _evict(self, processor: RequestProcessor, user_message: str) -> bool:
        """Concrete implementation of eviction process performed on a RequestProcessor."""
        # Set termination flag before restart to maintain eviction semantics
        processor._has_been_terminated = True

        # If a user has a job running on this processor at eviction, inform them with a detailed reason
        if processor.dispatched_task:
            status_message = "Request interrupted during execution."
            full_message = f"{status_message} {user_message}"
            processor.dispatched_task.respond_failure(full_message)
            processor.dispatched_task = None

        # Inform all queued tasks that their requests could not be processed due to eviction
        for task in processor._queue:
            status_message = "Request removed from queue."
            full_message = f"{status_message} {user_message}"
            task.respond_failure(description=full_message)

        processor._queue[:] = []  # ListProxy, so cannot use .clear()
        return True