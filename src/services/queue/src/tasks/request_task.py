from typing import Dict, Any, Optional
from .status import TaskStatus
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
        self._evicted = False


    @property
    def status(self) -> TaskStatus:
        """
        The status of the request in the queue lifecycle.
        """
        
        # This happens when calling .remote() fails
        if self._evicted:
            return TaskStatus.FAILED

        # Request must still be in the queue
        if self._future is None and self.position is not None:
            return TaskStatus.QUEUED

        # Request has been popped from the queue, but not yet dispatched
        elif self._future is None and self.position is None:
            return TaskStatus.PENDING

        # Request has been dispatched, check if it has completed
        try:
            # ray 2.47.0
            ready = self._future._fetch_future_result_sync(_timeout_s=0)
            if ready:
                return TaskStatus.COMPLETED
            
        except TimeoutError:
            return TaskStatus.DISPATCHED
        except Exception as e:
            self._log_error(f"Error checking request {self.id} status: {e}")
            return TaskStatus.FAILED


    def run(self, app_handle) -> bool:
        """
        Run the request using Ray backend in a separate thread.
        
        Args:
            app_handle: Ray app handle for the model deployment
            
        Returns:
            True if the request was successfully started, False otherwise
        """
        try:
            self.position = None
            self._log_debug(f"{self.data.id} Attempting to make remote() call with app handle")
            self._future = app_handle.remote(self.data)
            self._log_debug(f"{self.data.id} Succesfully made remote() call with app handle")
            return True
        except Exception as e:
            self._log_error(f"Error running request {self.id}: {e}")
            
            # This Naively assumes that the controller evicted the deployment
            self._evicted = True
            return False


    def respond(self, description : Optional[str] = None):
        """Creates a response for the task and handles routing to the appropriate locations."""

        # Handle creating a default response (regarding queue position) if description is None
        description = super().respond(description)

        response = self.data.create_response(
            status=ResponseModel.JobStatus.APPROVED,
            description=description,
            logger=logger,
        )

        try:
            response.respond()

        except Exception as e:
            self._log_error(f"Failed to respond back to user: {e}")


    def respond_failure(self, description: Optional[str] = None):
        """
        Respond to the user with a failure message.
        """
        if description is None:
            description = f"{self.id} - Dispatch failed!"

        response = self.data.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=description,
            logger=logger,
        )
        try:
            response.respond()
        except Exception as e:
            self._log_error(f"Failed to respond back to user: {e}")


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