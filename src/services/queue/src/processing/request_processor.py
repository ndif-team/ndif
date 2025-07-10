import logging
from datetime import datetime
from multiprocessing import Manager
from typing import Any, Dict, List, Optional

from ray import serve
from slugify import slugify

from ..schema import BackendRequestModel
from ..tasks.request_task import RequestTask
from .base import Processor
from .status import DeploymentStatus, ProcessorStatus

logger = logging.getLogger("ndif")


def slugify_model_key(model_key: str) -> str:
    """Slugify a model key. This places it in a format suitable to fetch from ray."""
    return f"Model:{slugify(model_key)}"


class RequestProcessor(Processor[RequestTask]):
    """
    Queue for making requests to model deployments using Ray backend.
    """

    def __init__(self, model_key: str, *args, **kwargs):
        super().__init__(*args, **kwargs)

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
                logger.debug(f"Successfully fetched app handle for {ray_model_key}")
                self.deployment_status = DeploymentStatus.DEPLOYED
            except Exception as e:
                logger.debug(f"Failed to get app handle for {self.model_key}..")
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

        scheduled_status = [
            DeploymentStatus.FREE,
            DeploymentStatus.FULL,
            DeploymentStatus.CACHED_AND_FREE,
            DeploymentStatus.CACHED_AND_FULL,
        ]

        # No attempt has been made to submit a request for this model to the controller.
        if self.deployment_status == DeploymentStatus.UNINITIALIZED:
            return ProcessorStatus.UNINITIALIZED

        elif self.deployment_status == DeploymentStatus.DEPLOYED:
            if self._has_been_terminated:
                return ProcessorStatus.TERMINATED
            if not self.dispatched_task and len(self._queue) == 0:
                return ProcessorStatus.INACTIVE
            if self.max_tasks and len(self.queue) >= self.max_tasks:
                return ProcessorStatus.DRAINING
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
            enqueued = super().enqueue(queue_item)
            if not enqueued:
                if self.status == ProcessorStatus.DRAINING:
                    queue_item.respond_failure(
                        self.sio,
                        self.object_store,
                        f"Queue has currently at max capacity of {self.max_tasks}, please try again later.",
                    )
                else:
                    queue_item.respond_failure(
                        self.sio,
                        self.object_store,
                        f"Request could not be enqueued for unknown reason.",
                    )

        except Exception as e:
            logger.error(f"{request.id} - Error enqueuing request: {e}")
            return False

        try:
            queue_item.respond(
                description=f"Your job has been added to the queue. Currently at position {position + 1}"
            )
        except Exception as e:
            logger.error(
                f"{request.id} - Error responding to user at queued stage: {e}"
            )

        return enqueued

    def notify_pending_task(self):
        """Helper method used to update user(s) waiting for model to be scheduled."""

        description = f"{self.model_key} is being scheduled... stand by"

        for pending_task in self._queue:

            pending_task.respond(description)

    def _dispatch(self) -> bool:
        """
        Dispatch a request using Ray backend.
        """

        logger.debug(f"Attempting to dispatch on {self.model_key}")
        try:
            self.dispatched_task.respond(description="Dispatching request...")
        except Exception as e:
            logger.error(f"Failed to respond to user about task being dispatched: {e}")
        success = self.dispatched_task.run(self.app_handle)
        if success:
            logger.debug(f"Succesfully dispatched on {self.model_key}")
            self.last_dispatched = datetime.now()
        return success

    # TODO: Come up with better name
    def _is_invariant_status(self, current_status: ProcessorStatus) -> bool:

        # Statuses which have nothing to do with tasks.
        invariant_statuses = [
            ProcessorStatus.UNINITIALIZED,
            ProcessorStatus.INACTIVE,
            ProcessorStatus.PROVISIONING,
            ProcessorStatus.UNAVAILABLE,
        ]

        return any(current_status == status for status in invariant_statuses)

    def update_positions(self, indices: Optional[List[int]] = None):
        """
        Update the positions of tasks in the queue. Overrides the base class to pass in the networking clients.

        Args:
            indices: Optional list of indices to update. If None, updates all.
        """

        indices = indices or range(len(self.queue))
        for i in indices:
            if i < len(self.queue):
                task = self.queue[i]
                task.position = i
                task.respond_position_update()

    def _handle_failed_dispatch(self):
        """
        Handle a failed request with Ray-specific logic.
        """

        if self.dispatched_task.retries < self.max_retries:
            # Try again
            logger.error(
                f"Request {self.dispatched_task.id} failed, retrying... (attempt {self.dispatched_task.retries + 1} of {self.max_retries})"
            )
            self._dispatch()
            self.dispatched_task.retries += 1
        else:
            try:
                # Try to inform the user that the request has failed
                description = f"Unable to reach {self.model_key}. This likely means that the deployment has been evicted."
                self.dispatched_task.respond_failure(description=description)
            except Exception as e:
                # Give up
                logger.error(
                    f"Error handling failed request {self.dispatched_task.id}: {e}"
                )

            self.dispatched_task = None
