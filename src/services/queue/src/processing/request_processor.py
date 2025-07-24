import os
import logging
from multiprocessing import Manager
import ray
from ray import serve
from ray.serve.exceptions import RayServeException
from slugify import slugify

from ..schema import BackendRequestModel
from ..tasks.request_task import RequestTask
from .base import Processor
from .status import DeploymentStatus

import time

logger = logging.getLogger("ndif")


def convert_to_ray_app_name(processor_id: str) -> str:
    """Convert a processor ID to a Ray app handle name format."""
    return f"Model:{slugify(processor_id)}"


class RequestProcessor(Processor[RequestTask]):
    """
    Queue for making requests to model deployments using Ray backend.
    """

    def __init__(self, model_key: str, *args, **kwargs):
        super().__init__(processor_id=model_key, *args, **kwargs)
        # Override the base class queue with multiprocessing Manager list
        self._queue = Manager().list()
        self._app_handle = None
        self.backend_status = DeploymentStatus.UNINITIALIZED

    @property
    def handle(self):
        """
        Get the Ray app handle for the model.
        """
        if not self._app_handle:
            try:
                ray_model_key = convert_to_ray_app_name(self.id)
                start = time.perf_counter()
                self._app_handle = serve.get_app_handle(ray_model_key)
                logger.debug(f"Successfully fetched app handle in {time.perf_counter() - start} seconds for {ray_model_key}")
                self.backend_status = DeploymentStatus.DEPLOYED
            except RayServeException as e:
                # ray 2.47.0
                if str(e) != f"Application '{ray_model_key}' does not exist.":
                    logger.debug(f"Ray exception: {e}")
                self._app_handle = None
            except Exception as e:
                logger.exception(f"Failed to get app handle for {self.id}..{e}")
                self._app_handle = None
        return self._app_handle

    def enqueue(self, request: BackendRequestModel) -> bool:
        """
        Enqueue a request with RequestTask-specific logic.
        """
        position = len(self.queue)
        queue_item = RequestTask(request.id, request, position)
        return super().enqueue(queue_item)

    def notify_pending_task(self):
        """Helper method used to update user(s) waiting for model to be scheduled."""
        description = f"`{self.id}` is being deployed... stand by."
        super().notify_pending_task(description)

    def _cancel_dispatched_task(self):
        """Cancel dispatched task with Ray-specific cancellation logic."""
        if self.dispatched_task:
            ray_cancel_timeout = float(os.environ.get("_RAY_CANCEL_TIMEOUT", 5.0))
            try:
                # Defensive: avoid blocking forever if app_handle is invalid
                cancel_ref = self.handle.cancel.remote()
                try:
                    _ = cancel_ref.result(timeout_s=ray_cancel_timeout)
                except Exception as timeout_exc:
                    logger.warning(
                        f"[PROCESSOR] {self.id} - Timed out or failed trying to cancel dispatched task {self.dispatched_task.id} via handle.cancel.remote() after {ray_cancel_timeout} seconds: {timeout_exc}"
                    )
                else:
                    logger.debug(
                        f"[PROCESSOR] {self.id} - Successfully called cancel.remote() for dispatched task {self.dispatched_task.id}."
                    )
            except Exception as e:
                logger.exception(
                    f"[PROCESSOR] {self.id} - Error attempting to cancel dispatched task {self.dispatched_task.id}: {e}"
                )

        # Perform common cleanup via base class
        super()._cancel_dispatched_task()

    def _get_failure_message(self) -> str:
        """
        Get Ray-specific failure message for deployment-related failures.
        """
        return f"Unable to reach {self.id}. This likely means that the deployment has been evicted."

    def _restart_implementation(self):
        """
        RequestProcessor-specific restart logic.
        
        Resets the processor to initial state by:
        - Setting backend_status to UNINITIALIZED
        - Setting app_handle to None
        - Reinitializing the queue to avoid multiprocessing broken pipe issues
        """
        logger.info(f"Restarting RequestProcessor for model {self.id}")
        self.backend_status = DeploymentStatus.UNINITIALIZED
        self._app_handle = None
        # Re-initialize with multiprocessing Manager list (base class will clear it first)
        self._queue = Manager().list()
