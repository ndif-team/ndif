import asyncio
import os
import time
import ray
from ray import serve
import threading
from typing import Dict, Optional, List, Any
from ..logging import set_logger
from ..schema import BackendRequestModel
from ..processing.request_processor import RequestProcessor
from ..processing.state import ProcessorState, DeploymentState
from .base import Coordinator
from .mixins import NetworkingMixin

logger = set_logger("Queue")


class RequestCoordinator(Coordinator[BackendRequestModel, RequestProcessor], NetworkingMixin):
    """
    Coordinates requests between the queue and the model deployments using Ray backend.
    """

    def __init__(self, tick_interval: float = 1.0, max_retries: int = 3, ray_url: str = None, 
                 sio=None, object_store=None):
        Coordinator.__init__(self, tick_interval, max_retries)
        NetworkingMixin.__init__(self, sio, object_store)
        self.ray_url = ray_url
        self.ray_connected = False
        self.ray_watchdog = threading.Thread(target=self.connect_to_ray, daemon=True)
        self.ray_watchdog.start()
        self._controller = None

    @property
    def controller(self):
        """Creates a Controller handle used to submit requests for models to deploy"""
        if self._controller is None:
            self._controller = serve.get_app_handle("Controller")
        return self._controller

    def state(self) -> Dict[str, Any]:
        """Get the state of the coordinator. Adds ray_connected to the base state."""
        base_state = super().state()
        base_state["ray_connected"] = self.ray_connected
        return base_state

    async def route_request(self, request: BackendRequestModel) -> bool:
        """Route request to appropriate processor. 
        
        Returns:
            True if request was routed successfully, False otherwise.
        """
        try:
            # Validate request
            if not request or not request.model_key:
                self._log_error("Invalid request: missing model_key")
                return False
            
            model_key = request.model_key
            
            # Try to route to existing active processor
            if model_key in self.active_processors:
                processor = self.active_processors[model_key]
                success = processor.enqueue(request)
                if success:
                    self._log_debug(f"Request {request.id} routed to active processor {model_key}")
                    return True
                else:
                    self._log_error(f"Failed to enqueue request {request.id} to active processor {model_key}")
                    return False

            # Try to route to inactive processor
            elif model_key in self.inactive_processors:
                processor = self.inactive_processors[model_key]
                processor.deployment_state = DeploymentState.UNINITIALIZED
                success = processor.enqueue(request)
                if success:
                    # Move processor to active state
                    self.active_processors[model_key] = processor
                    del self.inactive_processors[model_key]
                    self._log_info(f"Activated processor {model_key} and routed request {request.id}")
                    return True
                else:
                    self._log_error(f"Failed to enqueue request {request.id} to inactive processor {model_key}")
                    return False

            # Create new processor
            else:
                try:
                    processor = self._create_processor(model_key)
                    success = processor.enqueue(request)
                    if success:
                        self.active_processors[model_key] = processor
                        self._log_info(f"Created new processor {model_key} and routed request {request.id}")
                        return True
                    else:
                        self._log_error(f"Failed to enqueue request {request.id} to new processor {model_key}")
                        return False
                except Exception as e:
                    self._log_error(f"Failed to create processor {model_key}: {e}")
                    return False
                    
        except Exception as e:
            self._log_error(f"Error routing request {request.id if request else 'unknown'}: {e}")
            return False

    def _should_update_processor(self, processor: RequestProcessor) -> bool:
        """Determine if a processor should be updated."""
        try:
            if processor._needs_update:
                logger.debug(f"Processor {processor.model_key} needs update")
                processor.update_positions()
                return True
            else:
                return False
        except Exception as e:
            self._log_error(f"Error checking if processor should be updated: {e}")
            return False

    def _should_deactivate_processor(self, processor: RequestProcessor) -> bool:
        """Determine if a processor should be deactivated."""
        try:
            # Deactivate if processor is inactive
            if processor.status == ProcessorState.INACTIVE:
                return True
            
            return False
        except Exception as e:
            self._log_error(f"Error checking if processor should be deactivated: {e}")
            return True  # Deactivate on error

    def _should_deploy_processor(self, processor: RequestProcessor) -> bool:
        """Determine if a processor should be deployed."""
        try:
            if processor.status == ProcessorState.UNINITIALIZED and self.ray_connected:
                self._log_debug(f"Processor {processor.model_key} needs a deployment")
                return True
            else:
                return False
        except Exception as e:
            self._log_error(f"Error checking if processor should be deployed: {e}")
            return False

    def _processor_failed(self, processor: RequestProcessor) -> bool:
        """Determine whether processor has failed"""
        if processor.status in [ProcessorState.TERMINATED, ProcessorState.UNAVAILABLE]:
            return True
        return False

    def handle_processor_failure(self, processor: RequestProcessor):
        """
        Handle a failed processor by notifying users, clearing its queue,
        and moving it from active to inactive processors.
        """
        processor_state = processor.status

        if processor_state == ProcessorState.TERMINATED:
            description = (
                f"Deployment for {processor.model_key} has been terminated by the scheduler. "
                "You can request it to be rescheduled by re-running your nnsight script."
            )
        elif processor_state == ProcessorState.UNAVAILABLE:
            reason = processor.deployment_state
            if reason == DeploymentState.CANT_ACCOMMODATE:
                description = (
                    f"Cannot accommodate deployment for {processor.model_key}. "
                    "It is currently unavailable to be deployed on our cluster, either due to size, or issues loading. "
                    "If you believe this model should be supported, feel free to make a post on https://discuss.ndif.us/ "
                    "or raise a Github issue: https://github.com/ndif-team/ndif/issues"
                )
            else:
                self._log_error(f"Processor for {processor.model_key} was set to UNAVAILABLE, but the deployment state is: {reason}. This is unexpected.")
                description = "Deployment failed."
        else:
            self._log_error(f"Unknown processor failure: {processor_state}")
            description = "Deployment failed."

        # Notify all queued requests of the failure
        for request in processor._queue:
            self._log_debug(f"Failure notification for {request.id}")
            try:
                request.respond_failure(self.sio, self.object_store, description=description)
            except Exception as e:
                self._log_debug(f"Failed to respond failure: {e}")
        
        self._log_debug(f"Clearing queue for {processor.model_key}")
        processor._queue[:] = [] # ListProxy, so cannot use .clear()

        # Remove processor from queue
        self.active_processors.pop(processor.model_key, None)
        self.inactive_processors.pop(processor.model_key, None)
            

    def _deploy(self, processors: List[RequestProcessor]):
        """
        Attempts to deploy a list of RequestProcessors by invoking the controller's deploy method.
        Updates each processor's deployment state based on the controller's response.

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
            # The .get(3.14) is a hack to retrieve the result with a timeout
            deployment_statuses = deployment_results.get(3.14)

            logger.debug(f"Deployment results from controller: {deployment_statuses}")

            for processor in processors:
                # Retrieve and normalize the deployment status for this processor
                status_str = str(deployment_statuses[processor.model_key]).lower()
                try:
                    deployment_state = DeploymentState(status_str)
                except ValueError:
                    self._log_error(f"Unknown processor state '{status_str}' for model_key '{processor.model_key}'")
                    deployment_state = DeploymentState.CANT_ACCOMMODATE

                # Store the processor's deployment state in a clear attribute
                processor.deployment_state = deployment_state

        except Exception as e:
            self._log_error(f"Error during processor deployment: {e}")

    def _create_processor(self, processor_key: str) -> RequestProcessor:
        """Create a new RequestProcessor."""
        return RequestProcessor(processor_key, self.max_retries, self.sio, self.object_store)

    def get_processor_status(self, model_key: str) -> Optional[Dict]:
        """Get the status of a specific processor."""
        try:
            return super().get_processor_status(model_key)
        except Exception as e:
            self._log_error(f"Error getting processor status for {model_key}: {e}")
            return None

    def get_all_processors(self) -> List[Dict]:
        """Get status of all processors."""
        try:
            return super().get_all_processors()
        except Exception as e:
            self._log_error(f"Error getting all processors: {e}")
            return []

    def connect_to_ray(self):
        """Connect to Ray cluster."""
        retry_interval = int(os.environ.get("RAY_RETRY_INTERVAL_S", 5))
        while True:
            try:
                if not ray.is_initialized():
                    ray.shutdown()
                    serve.context._set_global_client(None)
                    ray.init(logging_level="error", address = self.ray_url)
                    time.sleep(3)
                    logger.info("Connected to Ray cluster.")
                    self.ray_connected = True
                    return
            except Exception as e:
                logger.error(f"Failed to connect to Ray cluster: {e}")
            time.sleep(retry_interval)

    # Override logging methods to use the service logger
    def _log_debug(self, message: str):
        """Log a debug message using the service logger."""
        logger.debug(message)

    def _log_info(self, message: str):
        """Log an info message using the service logger."""
        logger.info(message)

    def _log_warning(self, message: str):
        """Log a warning message using the service logger."""
        logger.warning(message)

    def _log_error(self, message: str):
        """Log an error message using the service logger."""
        logger.error(message)


# TODO: Introduce the concept of time-based states, and canonical ordering of states
# Then, can just need to return the T previous states which changed

class DevRequestCoordinator(RequestCoordinator):
    """
    A development coordinator that stores the T previous states of the coordinator.
    """

    def __init__(self, tick_interval: float = 1.0, max_retries: int = 3, ray_url: str = None, 
                 num_previous_states: int = 30, sio=None, object_store=None):
        super().__init__(tick_interval, max_retries, ray_url, sio, object_store)
        self.previous_states = []
        self.num_previous_states = num_previous_states

    def get_previous_states(self) -> List[Dict[str, Any]]:
        """Get the previous states of the coordinator."""
        return self.previous_states

    def _advance_processor_lifecycles(self):
        """Override to add state tracking."""

        # Call the parent method synchronously
        super()._advance_processor_lifecycles()

        # Add state tracking
        self.previous_states.append(self.state())
        if len(self.previous_states) > self.num_previous_states:
            self.previous_states.pop(0)

if os.environ.get("DEV_MODE", True):
    logger.info("Using DevRequestCoordinator")
    RequestCoordinator = DevRequestCoordinator