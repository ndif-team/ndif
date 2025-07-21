import os
import logging
from datetime import datetime
from multiprocessing import Manager
from typing import Any, Dict, List, Optional

import ray
from ray import serve
from ray.serve.exceptions import RayServeException
from slugify import slugify

from ..schema import BackendRequestModel
from ..tasks.request_task import RequestTask
from .base import Processor
from .status import DeploymentStatus, ProcessorStatus

import time

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
                start = time.perf_counter()
                self._app_handle = serve.get_app_handle(ray_model_key)
                logger.debug(f"Successfully fetched app handle in {time.perf_counter() - start} seconds for {ray_model_key}")
                self.deployment_status = DeploymentStatus.DEPLOYED
            except RayServeException as e:
                # ray 2.47.0
                if str(e) != f"Application '{ray_model_key}' does not exist.":
                    logger.debug(f"Ray exception: {e}")
                self._app_handle = None
            except Exception as e:
                logger.exception(f"Failed to get app handle for {self.model_key}..{e}")
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
            logger.exception(f"{request.id} - Error enqueuing request: {e}")
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

        description = f"`{self.model_key}` is being deployed... stand by."

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
            logger.exception(f"Failed to respond to user about task being dispatched: {e}")
        success = self.dispatched_task.run(self.app_handle)
        if success:
            logger.debug(f"Succesfully dispatched on {self.model_key}")
            self.last_dispatched = datetime.now()
        return success

    def _cancel_dispatched_task(self):
        if self.dispatched_task:
            ray_cancel_timeout = float(os.environ.get("_RAY_CANCEL_TIMEOUT", 5.0))
            try:
                # Defensive: avoid blocking forever if app_handle is invalid
                cancel_ref = self.app_handle.cancel.remote()
                try:
                    result = cancel_ref.result(timeout_s=ray_cancel_timeout)
                except Exception as timeout_exc:
                    logger.warning(
                        f"[PROCESSOR] {self.model_key} - Timed out or failed trying to cancel dispatched task {self.dispatched_task.id} via app_handle.cancel.remote() after {ray_cancel_timeout} seconds: {timeout_exc}"
                    )
                else:
                    logger.debug(
                        f"[PROCESSOR] {self.model_key} - Successfully called cancel.remote() for dispatched task {self.dispatched_task.id}."
                    )
            except Exception as e:
                logger.exception(
                    f"[PROCESSOR] {self.model_key} - Error attempting to cancel dispatched task {self.dispatched_task.id}: {e}"
                )

        # Clear the deletion queue
        self.deletion_queue.clear()

        # Update positions for remaining tasks
        self.update_positions()

    def _handle_failed_dispatch(self):
        if self.dispatched_task.retries < self.max_retries:
            # Try again
            logger.exception(
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
                logger.exception(
                    f"Error handling failed request {self.dispatched_task.id}: {e}"
                )
            self.dispatched_task = None

    def _restart_implementation(self):
        """
        RequestProcessor-specific restart logic.
        
        Resets the processor to initial state by:
        - Setting deployment_status to UNINITIALIZED
        - Setting app_handle to None
        - Setting _has_been_terminated to False
        - Reinitializing the queue to avoid multiprocessing broken pipe issues
        """
        logger.info(f"Restarting RequestProcessor for model {self.model_key}")
        self.deployment_status = DeploymentStatus.UNINITIALIZED
        self._app_handle = None
        self._has_been_terminated = False
        self._queue = Manager().list()
