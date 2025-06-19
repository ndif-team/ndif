import ray
import asyncio
import concurrent.futures
from typing import Dict, Any
from .state import TaskState
from .base import Task
from ..schema import BackendRequestModel
from ..logging import load_logger

logger = load_logger(service_name="QUEUE", logger_name="REQUEST_TASK")

class RequestTask(Task):
    """
    Request for a model deployment using Ray backend.
    """
    def __init__(self, request_id: str, request: BackendRequestModel, position: int):
        super().__init__(request_id, request, position)
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
            ready, _ = ray.wait([self._future], num_returns=1, timeout=0)
            if len(ready) > 0:
                return TaskState.COMPLETED
            else:
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
            # Run the async Ray operation in a separate thread
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(self._resolve_async, app_handle)
                self._future = future.result()  # This blocks until complete
            self._log_debug(f"Request {self.id} dispatched!")
            self.position = None
            return True
        except Exception as e:
            self._log_error(f"Error running request {self.id}: {e}")
            return False

    def _resolve_async(self, app_handle):
        """
        Resolve the async Ray operation in a separate thread.
        This method runs in its own event loop to avoid conflicts.
        """
        async def _async_operation():
            return await app_handle.remote(self.data)._to_object_ref()
        
        return asyncio.run(_async_operation())

    def respond(self):
        """
        Override the base respond method to provide Ray-specific response logic.
        """
        if self.position is not None:
            description = f"{self.id} - Moved to position {self.position + 1}"
        else:
            description = f"{self.id} - Status updated to {self.status}"
        
        logger.debug(description)
        # TODO: Create response object, send to client

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