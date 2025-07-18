import logging
import ray
from typing import Dict, Any, Optional
from .status import TaskStatus
from .base import Task
from ..schema import BackendRequestModel
from nnsight.schema.response import ResponseModel

logger = logging.getLogger("ndif")

class RequestTask(Task):
    """
    Request for a model deployment using Ray backend.
    """

    def __init__(self, request_id: str, request: BackendRequestModel, position: int):
        Task.__init__(self, request_id, request, position)
        self._future = None
        self._dispatch_failed = False


    @property
    def status(self) -> TaskStatus:
        """
        The status of the request in the queue lifecycle.
        """
        
        # This happens when calling .remote() fails
        if self._dispatch_failed:
            return TaskStatus.FAILED

        # Request must still be in the queue
        if self._future is None and self.position is not None:
            return TaskStatus.QUEUED

        # Request has been popped from the queue, but not yet dispatched
        elif self._future is None and self.position is None:
            return TaskStatus.PENDING

        # Request has been dispatched, check if it has completed
        try:
            result = self._future._to_object_ref_sync()
            ready, _ = ray.wait([result], timeout=0)
            if len(ready) > 0:
                logger.debug(f"[TASK:{self.id}] Request {self.id} completed")
                self._future = None
                return TaskStatus.COMPLETED
            else:
                raise TimeoutError()
        except TimeoutError:
            return TaskStatus.DISPATCHED

        except Exception as e:
            logger.exception(f"Error checking request {self.id} status: {e}")
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
            logger.info(f"[TASK:{self.id}] Starting remote execution")
            self._future = app_handle.remote(self.data)
            logger.info(f"[TASK:{self.id}] Successfully initiated remote execution")
            return True
        except Exception as e:
            logger.exception(f"[TASK:{self.id}] Failed to start remote execution: {e}")
            
            # This Naively assumes that the controller evicted the deployment
            self._dispatch_failed = True
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
            logger.exception(f"Failed to respond back to user: {e}")


    def respond_failure(self, description: Optional[str] = None):
        """
        Respond to the user with a failure message.
        """
        if description is None:
            description = f"{self.id} - Dispatch failed!"

        logger.debug(f"Responding failure to user with following description: {description}")
        response = self.data.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=description,
            logger=logger,
        )
        try:
            response.respond()
        except Exception as e:
            logger.exception(f"Failed to respond back to user: {e}")