from typing import Any, Dict, List, Optional

from ray import serve

from ..logging import set_logger
from ..processing.request_processor import RequestProcessor
from ..processing.status import DeploymentStatus, ProcessorStatus
from ..providers.ray import RayProvider
from ..schema import BackendRequestModel
from .base import Coordinator

logger = set_logger("Queue")


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
            self._controller = serve.get_app_handle("Controller")
        return self._controller

    def route_request(self, request: BackendRequestModel) -> bool:
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
                
                # TODO: Verify whether this is necessary. I had it prior to the controller returning evicted deployments from controller.deploy()
                processor.deployment_status = DeploymentStatus.UNINITIALIZED
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


    def _handle_processor_failure(self, processor: RequestProcessor):
        """
        Handle a failed processor by notifying users, clearing its queue,
        and moving it from active to inactive processors.
        """
        processor_status = processor.status

        if processor_status == ProcessorStatus.TERMINATED:
            reason = (
                f"Deployment for {processor.model_key} has been terminated by the scheduler. "
                "You can request it to be rescheduled by re-running your nnsight script."
            )
        elif processor_status == ProcessorStatus.UNAVAILABLE:
            task_status = processor.deployment_status
            if task_status == DeploymentStatus.CANT_ACCOMMODATE:
                reason = (
                    f"Cannot accommodate deployment for {processor.model_key}. "
                    "It is currently unavailable to be deployed on our cluster, either due to size, or issues loading. "
                    "If you believe this model should be supported, feel free to make a post on https://discuss.ndif.us/ "
                    "or raise a Github issue: https://github.com/ndif-team/ndif/issues"
                )
            else:
                self._log_error(f"Processor for {processor.model_key} was set to UNAVAILABLE, but the deployment status is: {task_status}. This is unexpected.")
                reason = "Processor unavailable for unknown reason."
        else:
            self._log_error(f"Unknown processor failure: {processor_status}")
            reason = "Processor failed in unknown state."

        # Notify all queued requests of the failure
        self.evict_processor(processor, reason=reason)
           

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
            # The .get(3.14) is a hack to retrieve the result with a timeout
            deployment_statuses, evictions = deployment_results.get(3.14).values()

            logger.debug(f"Deployment results from controller: {deployment_statuses}")

            for processor in processors:
                # Retrieve and normalize the deployment status for this processor
                status_str = str(deployment_statuses[processor.model_key]).lower()
                try:
                    deployment_status = DeploymentStatus(status_str)
                except ValueError:
                    self._log_error(f"Unknown processor status '{status_str}' for model_key '{processor.model_key}'")
                    deployment_status = DeploymentStatus.CANT_ACCOMMODATE

                # Store the processor's deployment status in a clear attribute
                processor.deployment_status = deployment_status
            
            reason =  "Controller evicted deployment in order to a schedule different model."
            for model_key in evictions:
                try:
                    self.evict_processor(model_key, reason=reason)
                    self._log_debug(f"Evicted {model_key}")
                except Exception as e:
                    self._log_error(f"Failed to evict {model_key}: {e}")

        except Exception as e:
            self._log_error(f"Error during processor deployment: {e}")


    def _create_processor(self, processor_key: str) -> RequestProcessor:
        """Create a new RequestProcessor."""
        return RequestProcessor(processor_key, self.max_retries)


    def _evict(self, processor: RequestProcessor, reason: str) -> bool:
        """Concrete implementation of eviction process performed on a RequestProcessor."""
        processor._has_been_terminated = True
        processor._app_handle = None
        processor.deployment_status = ProcessorStatus.UNINITIALIZED

        # If a user has a job running on this processor at eviction, inform them with a detailed reason
        if processor.dispatched_task:
            description = (
                f"Request could not complete because the deployment it was running on was evicted. "
                f"Model key: {processor.model_key}. Reason: {reason}"
            )
            processor.dispatched_task.respond_failure(description)
            processor.dispatched_task = None

        # Inform all queued tasks that their requests could not be processed due to eviction
        for task in processor._queue:
            description = (
                f"Request was still in the queue when its deployment was evicted and could not be processed. "
                f"Model key: {processor.model_key}. Reason: {reason}"
            )
            task.respond_failure(description=description)

        processor._queue[:] = []  # ListProxy, so cannot use .clear()
        return True

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

    
    # These aren't currently being used at all
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

