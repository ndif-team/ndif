from multiprocessing import Manager
from typing import Dict, Any, List
from datetime import datetime
from ray import serve
from slugify import slugify
from ..schema import BackendRequestModel
from ..logging import set_logger
from .base import Processor
from .status import ProcessorStatus, DeploymentStatus
from ..tasks.request_task import RequestTask
from ..tasks.status import TaskStatus
from ..coordination.mixins import NetworkingMixin

logger = set_logger("Queue")

def slugify_model_key(model_key: str) -> str:
    """Slugify a model key. This places it in a format suitable to fetch from ray."""
    return f"Model:{slugify(model_key)}"


class RequestProcessor(Processor[RequestTask], NetworkingMixin):
    """
    Queue for making requests to model deployments using Ray backend.
    """


    def __init__(self, model_key: str, max_retries: int = 3, sio=None, object_store=None):
        Processor.__init__(self, max_retries)
        NetworkingMixin.__init__(self, sio, object_store)
        self.model_key = model_key
        self._queue = Manager().list()
        self._app_handle = None
        self.deployment_status = DeploymentStatus.UNINITIALIZED
        self._has_been_terminated = False


    @property    
    def app_handle(self):
        """
        Get the app handle for the model.
        """
        if not self._app_handle:
            try:
                ray_model_key = slugify_model_key(self.model_key)
                self._app_handle = serve.get_app_handle(ray_model_key)
                self._log_debug(f"Successfully fetched app handle for {ray_model_key}")
                self.deployment_status = DeploymentStatus.DEPLOYED
            except Exception as e:
                self._log_debug(f"Failed to get app handle for {self.model_key}..")
                self._app_handle = None
        return self._app_handle


    @property
    def queue(self) -> List[RequestTask]:
        """
        Get the queue of requests.
        """
        return self._queue


    @property
    def status(self):
        """
        The status of the queue.
        """

        scheduled_status = [DeploymentStatus.FREE, DeploymentStatus.FULL, DeploymentStatus.CACHED_AND_FREE, DeploymentStatus.CACHED_AND_FULL]

        # No attempt has been made to submit a request for this model to the controller.
        if self.deployment_status == DeploymentStatus.UNINITIALIZED:
            return ProcessorStatus.UNINITIALIZED

        elif self.deployment_status == DeploymentStatus.DEPLOYED:
            if self._has_been_terminated:
                return ProcessorStatus.TERMINATED
            if not self.dispatched_task and len(self._queue) == 0:
                return ProcessorStatus.INACTIVE
            return ProcessorStatus.ACTIVE
            
        # The deployment has been scheduled, but might not be finished
        elif self.deployment_status in scheduled_status:
            # Still waiting
            if self.app_handle is None:
                return ProcessorStatus.PROVISIONING

            # The deployment completed - update deployment status
            else:
                self.deployment_status = DeploymentStatus.DEPLOYED
                return ProcessorStatus.ACTIVE
            
        # The controller was unable to schedule the model
        else:
            return ProcessorStatus.UNAVAILABLE


    def get_state(self) -> Dict[str, Any]:
        """
        Get the state of the queue.
        """
        base_state = super().get_state()
        base_state["model_key"] = self.model_key
        return base_state


    def enqueue(self, request: BackendRequestModel) -> bool:
        """
        Enqueue a request.
        """
        try:
            position = len(self._queue)
            queue_item = RequestTask(request.id, request, position)
            result = super().enqueue(queue_item)

        except Exception as e:
            self._log_error(f"{request.id} - Error enqueuing request: {e}")
            return False

        try:
            queue_item.respond(self.sio, self.object_store, description=f"Your job has been added to the queue. Currently at position {position + 1}")
        except Exception as e:
            self._log_error(f"{request.id} - Error responding to user at queued stage: {e}")

        return result
    

    # TODO: This seems like it will double notify.
    def notify_pending_task(self):

        description = f"{self.model_key} is being scheduled... stand by"

        for pending_task in self._queue:

            pending_task.respond(
                self.sio, 
                self.object_store,
                description
            )


    def _dispatch(self) -> bool:
        """
        Dispatch a request using Ray backend.
        """

        self._log_debug(f"Attempting to dispatch on {self.model_key}")
        try:
            self.dispatched_task.respond(self.sio, self.object_store, description="Dispatching request..." )
        except Exception as e:
            self._log_error(f"Failed to respond to user about task being dispatched: {e}")
        success = self.dispatched_task.run(self.app_handle)
        if success:
            self._log_debug(f"Succesfully dispatched on {self.model_key}")
            self.last_dispatched = datetime.now()
        return success


    # TODO: Come up with better name
    def _is_invariant_status(self, current_status : ProcessorStatus) -> bool:

        # Statuses which have nothing to do with tasks.
        invariant_statuses = [
            ProcessorStatus.UNINITIALIZED, 
            ProcessorStatus.INACTIVE,
            ProcessorStatus.PROVISIONING,
            ProcessorStatus.UNAVAILABLE,
        ]

        return any(current_status == status for status in invariant_statuses)
        
    def _get_queued_status(self):
        """Return the queued status constant."""
        return TaskStatus.QUEUED

    def _get_pending_status(self):
        """Return the pending status constant."""
        return TaskStatus.PENDING

    def _get_dispatched_status(self):
        """Return the dispatched status constant."""
        return TaskStatus.DISPATCHED

    def _get_completed_status(self):
        """Return the completed status constant."""
        return TaskStatus.COMPLETED

    def _get_failed_status(self):
        """Return the failed status constant."""
        return TaskStatus.FAILED

    
    def _update_position(self, position: int):
        """
        Update the position of a task. Overrides the base class to pass in the networking clients.
        """
        task = self.queue[position]
        task.update_position(position).respond_position_update(self.sio, self.object_store)

    
    def _handle_failed_dispatch(self):
        """
        Handle a failed request with Ray-specific logic.
        """
        if self.dispatched_task.is_retryable(self.max_retries):
            # Try again
            self._log_error(f"Request {self.dispatched_task.id} failed, retrying... (attempt {self.dispatched_task.retries + 1} of {self.max_retries})")
            self._dispatch()
            self.dispatched_task.increment_retries()
        else:
            try: 
                # Try to inform the user that the request has failed
                description = f"Unable to reach {self.model_key}. This likely means that the deployment has been evicted."
                self.dispatched_task.respond_failure(self.sio, self.object_store, description=description)
            except Exception as e:
                # Give up
                self._log_error(f"Error handling failed request {self.dispatched_task.id}: {e}")

            self.dispatched_task = None

    # Override logging methods to use the service logger
    def _log_debug(self, message: str):
        """Log a debug message using the service logger."""
        logger.debug(message)

    def _log_error(self, message: str):
        """Log an error message using the service logger."""
        logger.error(message)

    def _log_warning(self, message: str):
        """Log a warning message using the service logger."""
        logger.warning(message)

    def _log_info(self, message: str):
        """Log an info message using the service logger."""
        logger.info(message)
