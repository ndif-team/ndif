import logging
from typing import Optional
from .status import TaskStatus
from .base import Task
from common.providers.ray import RayProvider
from common.schema import BackendRequestModel
from nnsight.schema.response import ResponseModel

logger = logging.getLogger("ndif")


class RequestTask(Task):
    """
    Request for a model deployment using Ray backend.
    """

    def __init__(self, request_id: str, request: BackendRequestModel, position: int):
        super().__init__(request_id, request, position)
        self._future = None

    @property
    def status(self) -> TaskStatus:
        """
        The status of the request in the queue lifecycle.
        """

        # This happens when calling .remote() fails
        if self._failed:
            return TaskStatus.FAILED

        # Request must still be in the queue
        if self._future is None and self.position is not None:
            return TaskStatus.QUEUED

        # Request has been popped from the queue, but not yet dispatched
        elif self._future is None and self.position is None:
            return TaskStatus.PENDING

        # Request has been dispatched, first ensure there is still a connection to Ray
        if not self.connected:
            self.respond_failure(
                "Disconnected from Ray controller, so cannot run task."
            )
            return TaskStatus.FAILED

        # check if it has completed
        try:
            _ = self._future.result(timeout_s=0, _skip_asyncio_check=True)
            logger.debug(f"[TASK:{self.id}] Request {self.id} completed")
            self._future = None
            return TaskStatus.COMPLETED
        except TimeoutError:
            return TaskStatus.DISPATCHED

        except Exception as e:
            error_msg = f"Error checking request {self.id} status: {e}"
            self.respond_failure(error_msg, traceback=True)
            return TaskStatus.FAILED

    @property
    def connected(self) -> bool:
        """Check if the task is connected to the Ray controller."""
        try:
            return RayProvider.connected()
        except Exception as e:
            logger.exception(f"Error checking Ray connection: {e}")
            return False

    def __str__(self):
        return f"RequestTask({self.id})"

    def run(self, app_handle) -> bool:
        """
        Run the request using Ray backend in a separate thread.

        Args:
            app_handle: Ray app handle for the model deployment

        Returns:
            True if the request was successfully started, False otherwise
        """

        # Avoid running app_handle if disconnected from Ray controller (runtime will hang indefinitely otherwise)
        if not self.connected or app_handle is None:
            self._failed = True
            self.respond_failure(
                f"[{str(self)}] Disconnected from Ray controller, so cannot run task."
            )
            return False

        self.position = None
        logger.info(f"[{str(self)}] Starting remote execution")
        try:
            self._future = app_handle.remote(self.data)
        except Exception as e:
            error_msg = f"[{str(self)}] Failed to start remote execution: {e}"
            # This naively assumes that the controller evicted the deployment
            self.respond_failure(error_msg, traceback=True)
            return False

        logger.info(f"[{str(self)}] Successfully initiated remote execution")
        return True

    def respond(
        self,
        description: Optional[str] = None,
        status: ResponseModel.JobStatus = ResponseModel.JobStatus.QUEUED,
    ):
        """Creates a response for the task and handles routing to the appropriate locations."""

        # Handle creating a default response (regarding queue position) if description is None
        description = super().respond(description)

        response = self.data.create_response(
            status=status,
            description=description,
            logger=logger,
        )

        try:
            response.respond()

        except Exception as e:
            logger.exception(f"Failed to respond back to user: {e}")

    def respond_failure(
        self, description: Optional[str] = None, traceback: bool = False
    ):
        """
        Respond to the user with a failure message.
        """
        # Let the base class handle the failure state management
        description = super().respond_failure(description, traceback)

        logger.debug(
            f"Responding failure to user with following description: {description}"
        )
        response = self.data.create_response(
            status=ResponseModel.JobStatus.ERROR,
            description=description,
            logger=logger,
        )
        try:
            response.respond()
        except Exception as e:
            logger.exception(f"Failed to respond back to user: {e}")
        finally:
            self._future = None
