"""Per-model request queue and execution processor.

`Processor` instances are keyed by `model_key` and manage the lifecycle of
requests targeting that model:
- Queue incoming `BackendRequestModel` items
- Track deployment availability and transition through states
- Dispatch requests to the Ray Serve deployment via `Handle`
- Poll for completion and respond to clients
"""

import logging
import os
import time
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import ray
from ray import serve
from ray.serve.handle import DeploymentResponse

from ..schema import BackendRequestModel, BackendResponseModel
from .handle import Handle

logger = logging.getLogger("ndif")


class ProcessorStatus(Enum):
    """States for the per-model processor lifecycle."""

    UNINITIALIZED = "uninitialized"
    PROVISIONING = "provisioning"
    DEPLOYING = "deploying"
    READY = "ready"
    BUSY = "busy"


@dataclass
class Submission:
    """Tracks an in-flight submission and its response future."""

    request: BackendRequestModel
    execution_future: DeploymentResponse


class Processor:
    """Manages a per-model queue and talks to the Ray backend via `Handle`."""

    def __init__(self, model_key: str):

        self.model_key = model_key

        self.queue: list[BackendRequestModel] = list()

        self.status = ProcessorStatus.UNINITIALIZED

        self.handle: Handle = None
        self.submission: Submission = None

        self.reply_freq_s = int(
            os.environ.get("COORDINATOR_PROCESSOR_REPLY_FREQ_S", "3")
        )

        self.last_reply_time: float = 0

    def enqueue(self, request: BackendRequestModel):
        """Add a request to the local queue and immediately update queue position for the user."""
        self.queue.append(request)

        # Update the user with their new position in the queue.
        request.create_response(
            BackendResponseModel.JobStatus.QUEUED,
            logger,
            f"Moved to position {len(self.queue)} in Queue.",
        ).respond()

    def step(self):
        """Advance the processor state machine by one tick."""

        # UNINITIALIZED: Before even being sent to deploy.
        # This branch should never be hit.
        if self.status == ProcessorStatus.UNINITIALIZED:
            self.reply("Uninitialized...")

        # PROVISIONING: Waiting for the controller to respond with the deployment status.
        # (I.e. checking to see whether the deployment can be created.)
        elif self.status == ProcessorStatus.PROVISIONING:
            self.reply("Model Deployment Provisioning...")

        # DEPLOYING: Waiting for the deployment to be ready.
        # (I.e. checking to see whether the model is finished loading.)
        elif self.status == ProcessorStatus.DEPLOYING:
            self.check_deployment()

        # READY: The model is ready to accept requests.
        # (I.e. the model is loaded and the deployment is ready with no active requests running.)
        elif self.status == ProcessorStatus.READY:

            # If there are requests in the queue, execute the next one.
            if len(self.queue) > 0:
                self.execute()

        # BUSY: The model is busy executing a request.
        elif self.status == ProcessorStatus.BUSY:
            # Poll the in-flight submission and transition to READY when done.
            self.check_submission()

    def execute(self):
        """Submit the next request to the model deployment via `Handle`."""

        request = self.queue.pop(0)

        # Submit the request to the model deployment via `Handle`.
        try:

            result = self.handle.execute(request)

            self.submission = Submission(
                request,
                result,
            )
        # If there is an error submitting the request, respond to the user with an error.
        except Exception as e:

            if isinstance(e, LookupError):
                message = "Model deployment evicted. Please try again later. Sorry for the inconvenience."
            else:
                message = f"{traceback.format_exc()}\nIssue submitting job to model deployment."

            request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                message,
            ).respond()

        else:

            # The request was submitted successfully, so we can transition to BUSY.
            self.status = ProcessorStatus.BUSY

            # Update the user with the request being dispatched to the model deployment.
            request.create_response(
                BackendResponseModel.JobStatus.DISPATCHED,
                logger,
                "Your job has been sent to the model deployment.",
            ).respond()

            # Update the other users in the queue with their new position in the queue.
            self.reply(force=True)

    def check_deployment(self):
        """Poll deployment readiness and transition to READY when available."""

        # If the handle is not yet created, create it.
        if self.handle is None:

            try:
                self.handle = Handle(self.model_key)
            # If there is a RayServeException, its okay and means the deployment stub hasn't been created yet.
            # Update the users that the model is still deploying.
            except serve.exceptions.RayServeException as e:
                self.reply("Model Deploying...")
                return

        # If the deployment is finished loading, transition to READY.
        if self.handle.ready:
            self.status = ProcessorStatus.READY
            self.step()

        # If the deployment is not finished loading, update the users that the model is still deploying.
        else:
            self.reply("Model Deploying...")

    def check_submission(self):
        """Poll the in-flight submission and transition to READY when done."""

        try:
            # Check the status of the in-flight submission.
            ray.get(self.submission.execution_future, timeout=0)
        except TimeoutError:
            # If the submission is still in progress, continue polling.
            return

        # If there is an error checking the status of the in-flight submission, respond to the user with an error.
        except Exception as e:

            request = self.submission.request

            self.status = ProcessorStatus.READY
            self.submission = None
            
            if isinstance(e, LookupError):
                message = "Model deployment evicted. Please try again later. Sorry for the inconvenience."
            else:
                message = f"{traceback.format_exc()}\nIssue checking job status."

            request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                message,
            ).respond()

            # Re-raise the error to be handled by the coordinator to check for connection issues.
            raise

        else:
            # The submission is complete, so we can transition to READY.
            self.status = ProcessorStatus.READY
            self.submission = None
            self.step()

    def reply(
        self,
        description: str | None = None,
        force: bool = False,
        status: BackendResponseModel.JobStatus = BackendResponseModel.JobStatus.QUEUED,
    ):
        """
        Reply to all users with a message.

        Args:
            description (str | None, optional): The message to send to users.
                If None, a default queue position message is sent.
            force (bool, optional): If True, force sending replies to all users
                regardless of last reply time. Defaults to False.
        """

        if force or time.time() - self.last_reply_time > self.reply_freq_s:

            for i, request in enumerate(self.queue):
                request.create_response(
                    status,
                    logger,
                    (
                        description
                        if description is not None
                        else f"Moved to position {i+1} in Queue."
                    ),
                ).respond()

            self.last_reply_time = time.time()

    def purge(self, message: Optional[str] = None):
        """Flush the queue and respond to all queued requests with `message`."""

        if message is None:
            message = "Critical server error occurred. Please try again later. Sorry for the inconvenience."

        self.reply(message, force=True, status=BackendResponseModel.JobStatus.ERROR)

        self.queue.clear()
