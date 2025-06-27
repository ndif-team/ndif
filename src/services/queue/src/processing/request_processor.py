from multiprocessing import Manager
from typing import Dict, Any, List
from datetime import datetime
from ray import serve
from slugify import slugify
from ..schema import BackendRequestModel
from ..logging import set_logger
from .base import Processor
from .state import ProcessorState, DeploymentState
from ..tasks.request_task import RequestTask
from ..tasks.state import TaskState
from ..coordination.mixins import NetworkingMixin

logger = set_logger("Queue")

def slugify_model_key(model_key: str) -> str:
    """Slugify a model key."""
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
        self.deployment_state = DeploymentState.UNINITIALIZED
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
                self.deployment_state = DeploymentState.DEPLOYED
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

        scheduled_states = [DeploymentState.FREE, DeploymentState.FULL, DeploymentState.CACHED_AND_FREE, DeploymentState.CACHED_AND_FULL]

        # No attempt has been made to submit a request for this model to the controller.
        if self.deployment_state == DeploymentState.UNINITIALIZED:
            return ProcessorState.UNINITIALIZED

        elif self.deployment_state == DeploymentState.DEPLOYED:
            if self._has_been_terminated:
                return ProcessorState.TERMINATED
            if not self.dispatched_task and len(self._queue) == 0:
                return ProcessorState.INACTIVE
            return ProcessorState.ACTIVE
            
        # The deployment has been scheduled, but might not be finished
        elif self.deployment_state in scheduled_states:
            # Still waiting
            if self.app_handle is None:
                return ProcessorState.PROVISIONING

            # The deployment completed - update deployment state
            else:
                self.deployment_state = DeploymentState.DEPLOYED
                return ProcessorState.ACTIVE
            
        # The controller was unable to schedule the model
        else:
            return ProcessorState.UNAVAILABLE


    def enqueue(self, request: BackendRequestModel) -> bool:
        """
        Enqueue a request.
        """
        try:
            queue_item = RequestTask(request.id, request, len(self._queue))
            return super().enqueue(queue_item)

        except Exception as e:
            self._log_error(f"Error enqueuing request {request.id}: {e}")
            return False

    def state(self) -> Dict[str, Any]:
        """
        Get the state of the queue.
        """
        base_state = super().state()
        base_state["model_key"] = self.model_key
        return base_state

    def _is_invariant_state(self, current_state : ProcessorState) -> bool:

        # States which have nothing to do with tasks.
        invariant_states = [
            ProcessorState.UNINITIALIZED, 
            ProcessorState.INACTIVE,
            ProcessorState.PROVISIONING,
            ProcessorState.UNAVAILABLE,
        ]

        return any(current_state == state for state in invariant_states)
        
    def _get_queued_state(self):
        """Return the queued state constant."""
        return TaskState.QUEUED

    def _get_pending_state(self):
        """Return the pending state constant."""
        return TaskState.PENDING

    def _get_dispatched_state(self):
        """Return the dispatched state constant."""
        return TaskState.DISPATCHED

    def _get_completed_state(self):
        """Return the completed state constant."""
        return TaskState.COMPLETED

    def _get_failed_state(self):
        """Return the failed state constant."""
        return TaskState.FAILED

    def _dispatch(self) -> bool:
        """
        Dispatch a request using Ray backend.
        """

        self._log_debug(f"Attempting to dispatch on {self.model_key}")
        success = self.dispatched_task.run(self.app_handle)
        if success:
            self._log_debug(f"Succesfully dispatched on {self.model_key}")
            self.last_dispatched = datetime.now()
        return success

    def _update_position(self, position: int):
        """
        Update the position of a task. Overrides the base class to pass in the networking clients.
        """
        task = self.queue[position]
        task.update_position(position).respond_position_update(self.sio, self.object_store)

    def notify_pending_task(self):

        description = f"{self.model_key} is being scheduled... stand by"

        for pending_task in self._queue:

            pending_task.respond(
                self.sio, 
                self.object_store,
                description
            )

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
