from multiprocessing import Manager
from typing import Dict, Any, List
from datetime import datetime
from ray import serve
from ..schema import BackendRequestModel
from ..logging import load_logger
from .base import Processor
from .state import ProcessorState
from ..tasks.request_task import RequestTask
from ..tasks.state import TaskState
from ..coordination.mixins import NetworkingMixin

logger = load_logger(service_name="QUEUE", logger_name="REQUEST_PROCESSOR")

class RequestProcessor(Processor[RequestTask], NetworkingMixin):
    """
    Queue for making requests to model deployments using Ray backend.
    """
    def __init__(self, model_key: str, max_retries: int = 3, sio=None, object_store=None):
        Processor.__init__(self, max_retries)
        NetworkingMixin.__init__(self, sio, object_store)
        self.model_key = model_key
        self._queue = Manager().list()

    @property    
    def app_handle(self):
        """
        Get the app handle for the model.
        """
        if not hasattr(self, '_app_handle'):
            try:
                self._app_handle = serve.get_app_handle(self.model_key)
            except Exception as e:
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
        
        # App handle fails to be created (meaning either the model is not deployed or there is an error trying to connect)
        if self.app_handle is None:
            return ProcessorState.DISCONNECTED

        # Queue is inactive if there are no requests and no dispatched request
        if not self.dispatched_task and len(self._queue) == 0:
            return ProcessorState.INACTIVE

        return ProcessorState.ACTIVE

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

    def _is_inactive(self, status) -> bool:
        """
        Check if the processor is inactive.
        """
        return status == ProcessorState.INACTIVE

    def _is_disconnected(self, status) -> bool:
        """
        Check if the processor is disconnected.
        """
        return status == ProcessorState.DISCONNECTED

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
        success = self.dispatched_task.run(self.app_handle)
        if success:
            self.last_dispatch_time = datetime.now()
        return success

    def _update_position(self, position: int):
        """
        Update the position of a task. Overrides the base class to pass in the networking clients.
        """
        task = self.queue[position]
        task.update_position(position).respond(self.sio, self.object_store)

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
                self.dispatched_task.respond(self.sio, self.object_store)
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
