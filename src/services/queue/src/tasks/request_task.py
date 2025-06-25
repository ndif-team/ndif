from typing import Dict, Any
from .state import TaskState
from .base import Task
from ..schema import BackendRequestModel
from nnsight.schema.response import ResponseModel
from ..logging import set_logger

logger = set_logger("Queue")

class RequestTask(Task):
    """
    Request for a model deployment using Ray backend.
    """
    def __init__(self, request_id: str, request: BackendRequestModel, position: int):
        Task.__init__(self, request_id, request, position)
        self._future = None

    @property
    def status(self) -> TaskState:
        """
        The status of the request in the queue lifecycle.
        """
        # Request must still be in the queue
        if self._future is None and self.position is not None:
            return TaskState.QUEUED

        # Request has been popped from the queue, but not yet dispatched
        elif self._future is None and self.position is None:
            return TaskState.PENDING

        # Request has been dispatched, check if it has completed
        try:
            # ray 2.47.0
            ready = self._future._fetch_future_result_sync(_timeout_s=0)
            if ready:
                return TaskState.COMPLETED
            
        except TimeoutError:
            return TaskState.DISPATCHED
        except Exception as e:
            self._log_error(f"Error checking request {self.id} status: {e}")
            return TaskState.FAILED

    def run(self, app_handle) -> bool:
        """
        Run the request using Ray backend in a separate thread.
        
        Args:
            app_handle: Ray app handle for the model deployment
            
        Returns:
            True if the request was successfully started, False otherwise
        """
        try:
            self._future = app_handle.remote(self.data)
            self.position = None
            return True
        except Exception as e:
            self._log_error(f"Error running request {self.id}: {e}")
            return False

    def respond(self, sio: "socketio.SimpleClient", object_store: "boto3.client"):
        """
        Override the base respond method to provide Ray-specific response logic.
        """

        if self.status == TaskState.FAILED:        
            # TODO: More informative description.
            job_status = ResponseModel.JobStatus.ERROR
            description = f"{self.id} - Dispatch failed!"
        
        else:
            job_status = ResponseModel.JobStatus.APPROVED
            if self.position is not None:
                description = f"{self.id} - Moved to position {self.position + 1}"
            else:
                description = f"{self.id} - Status updated to {self.status}"
        
        logger.debug(description)
        
        # Create and send response if networking clients are available
        response = self.data.create_response(
            status=job_status,
            description=description,
            logger=logger,
        )
        response.respond(sio, object_store)

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