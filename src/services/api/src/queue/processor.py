import asyncio
import logging
import os
from enum import Enum
from typing import Optional

import ray

from ..schema import BackendRequestModel, BackendResponseModel

from .util import controller_handle, get_actor_handle, submit

logger = logging.getLogger("ndif")


class ProcessorStatus(Enum):
    UNINITIALIZED = "uninitialized"
    PROVISIONING = "provisioning"
    DEPLOYING = "deploying"
    READY = "ready"
    BUSY = "busy"
    CANCELLED = "cancelled"


class DeploymentStatus(Enum):
    """Deployment states reported by the controller."""

    DEPLOYED = "deployed"
    CACHED_AND_FREE = "cached_and_free"
    FREE = "free"
    CACHED_AND_FULL = "cached_and_full"
    FULL = "full"
    CANT_ACCOMMODATE = "cant_accommodate"


class Processor:
    """
    Orchestrates per-model request queues, deployment lifecycle, and communication with backend model actors.

    The Processor manages a queue of inference requests for a specific model, handles deployment
    provisioning, monitors readiness and status, and relays requests to the model actor. It reports
    errors and eviction events to the Dispatcher via shared queues.
    """

    def __init__(
        self, model_key: str, eviction_queue: asyncio.Queue, error_queue: asyncio.Queue
    ):
        self.model_key = model_key
        self.queue: asyncio.Queue[BackendRequestModel] = asyncio.Queue()
        self.eviction_queue = eviction_queue
        self.error_queue = error_queue
        self.status = ProcessorStatus.UNINITIALIZED

        self.dedicated = None

    @property
    def handle(self) -> ray.actor.ActorHandle:
        """Get the handle for the model deployment."""
        return get_actor_handle(f"ModelActor:{self.model_key}")

    def enqueue(self, request: BackendRequestModel):
        """Add a request to the queue and update the user with their position in the queue."""

        if self.dedicated is False and not request.hotswapping:
            request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                "Model is not dedicated and hotswapping is not supported for this API key. See https://nnsight.net/status/ for a list of scheduled models.",
            ).respond()

            return

        self.queue.put_nowait(request)

        # Update the user with their new position in the queue.
        request.create_response(
            BackendResponseModel.JobStatus.QUEUED,
            logger,
            f"Moved to position {self.queue.qsize()} in Queue.",
        ).respond()

    async def check_dedicated(self, handle: ray.actor.ActorHandle) -> bool:
        result = await submit(handle, "get_deployment", self.model_key)

        if result is None:
            return False

        return result.get("dedicated", False)

    async def provision(self):
        """Provision the model deployment by contacting the Controller to create the model deployment."""

        try:
            # Get the controller handle.
            controller = controller_handle()

            self.dedicated = await self.check_dedicated(controller)

            if not self.dedicated:
                hotswap = False

                valid_queue = list()

                while not self.queue.empty():
                    request = self.queue.get_nowait()

                    if request.hotswapping:
                        hotswap = True

                        valid_queue.append(request)
                    else:
                        request.create_response(
                            BackendResponseModel.JobStatus.ERROR,
                            logger,
                            "Model is not dedicated and hotswapping is not supported for this API key. See https://nnsight.net/status/ for a list of scheduled models.",
                        ).respond()

                for request in valid_queue:
                    self.queue.put_nowait(request)

                if not hotswap:
                    self.eviction_queue.put_nowait(
                        (
                            self.model_key,
                            "Model is not dedicated and hotswapping is not supported for this API key. See https://nnsight.net/status/ for a list of scheduled models.",
                        )
                    )
                    self.status = ProcessorStatus.CANCELLED
                    return

            # Submit the request to the controller to deploy the model deployment to the controller.
            # Wait for the request to finish.
            result = await submit(controller, "deploy", [self.model_key])

        # If there was an error provisioning the model deployment, evict and cancel the processor.
        # Add the error to the error queue to be handled by the dispatcher.
        except Exception as e:
            self.eviction_queue.put_nowait(
                (
                    self.model_key,
                    "Error provisioning model deployment. Please try again later. Sorry for the inconvenience.",
                )
            )
            self.status = ProcessorStatus.CANCELLED
            self.error_queue.put_nowait((self.model_key, e))

            return

        deployment_statuses = result["result"]

        evictions = result["evictions"]

        # Add any evictions to the eviction queue to be handled by the dispatcher.
        for model_key in evictions:
            self.eviction_queue.put_nowait(
                (
                    model_key,
                    "Model deployment evicted. Please try again later. Sorry for the inconvenience.",
                )
            )

        for model_key, status in deployment_statuses.items():
            status_str = str(status).lower()

            try:
                deployment_status = DeploymentStatus(status_str)

            # If the status is not a valid deployment status, there was an issue evaluating the model on the Controller.
            # Evict and cancel the processor.
            except ValueError:
                self.eviction_queue.put_nowait(
                    (
                        model_key,
                        f"{status_str}\n\nThere was an error provisioning the model deployment. Please try again later. Sorry for the inconvenience.",
                    )
                )
                self.status = ProcessorStatus.CANCELLED

                return

            # If the model deployment cannot be accommodated, evict and cancel the processor.
            if deployment_status == DeploymentStatus.CANT_ACCOMMODATE:
                self.eviction_queue.put_nowait(
                    (
                        model_key,
                        "Model deployment cannot be accomodated at this time. Please try again later. Sorry for the inconvenience.",
                    )
                )
                self.status = ProcessorStatus.CANCELLED

                return

    async def initialize(self) -> None:
        """Wait for the model deployment to complete initialization."""

        # Loop until the model deployment is initialized.
        while True:
            try:
                # Try and get the handle for the model deployment. It might not be created yet.
                handle = self.handle

                # Wait for the model deployment to be initialized.
                await submit(handle, "__ray_ready__")

                break

            except Exception as e:
                # If the error is not becuase the model deployment hasnt been created yet, its a critical error,
                # Cancel the processor and let the dispatcher handle the error.
                if not str(e).startswith("Failed to look up actor"):
                    self.eviction_queue.put_nowait(
                        (
                            self.model_key,
                            "Error initializing model deployment. Please try again later. Sorry for the inconvenience.",
                        )
                    )
                    self.error_queue.put_nowait((self.model_key, e))
                    self.status = ProcessorStatus.CANCELLED

    async def execute(self, request: BackendRequestModel) -> None:
        """Submit a request to the model deployment and update the user with the status."""

        try:
            # Get the handle for the model deployment.
            handle = self.handle

            # Submit the request to the model deployment.
            result = submit(handle, "__call__", request)

            # Update the user their request has been dispatched.
            request.create_response(
                BackendResponseModel.JobStatus.DISPATCHED,
                logger,
                "Your job has been sent to the model deployment.",
            ).respond()

            # Wait for the request to be completed
            result = await result

        # If there was an error submitting the request...
        except Exception as e:
            # Respond to the dispatched user with an error.
            request.create_response(
                BackendResponseModel.JobStatus.ERROR,
                logger,
                "Error submitting request to model deployment. Please try again later. Sorry for the inconvenience.",
            ).respond()

            # If the error is because the model deployment was evicted / cached, evict and cancel the processor.
            if str(e).startswith("Failed to look up actor"):
                self.eviction_queue.put_nowait(
                    (
                        self.model_key,
                        "Model deployment evicted. Please try again later. Sorry for the inconvenience.",
                    )
                )
                self.status = ProcessorStatus.CANCELLED
            else:
                # If there is another error, add it ot the error queue to be handled by the dispatcher. Remain busy until the dispatcher has cleared the error.
                self.error_queue.put_nowait((self.model_key, e))

        # Otherwise the processor is ready to accept new requests.
        else:
            self.status = ProcessorStatus.READY

    async def processor_worker(self) -> None:
        """Main asyncio task for creating, monitoring and submitting requests to the a model deployment."""

        self.status = ProcessorStatus.PROVISIONING

        # Create a task to reply to users with statts of model deployment.
        asyncio.create_task(self.reply_worker())

        # Provision and deploy the model deployment.
        await self.provision()

        # If there was a problem provisioning the model deployment, return.
        if self.status == ProcessorStatus.CANCELLED:
            return

        self.status = ProcessorStatus.DEPLOYING

        # Wait for the model deployment to be initialized.
        await self.initialize()

        # If there was a problem initializing the model deployment, return.
        if self.status == ProcessorStatus.CANCELLED:
            return

        self.status = ProcessorStatus.READY

        # Loop until the model deployment is cancelled.
        while self.status != ProcessorStatus.CANCELLED:
            # If there was previously an error executing, wait for the dispatcher to check and clear the error.
            if self.status == ProcessorStatus.BUSY:
                await asyncio.sleep(1)

                continue

            # Get the next request from the queue.
            request = await self.queue.get()

            self.status = ProcessorStatus.BUSY

            # Update the other users in the queue with their new position in the queue.
            self.reply()

            # Submit the request to the model deployment.
            await self.execute(request)

    async def reply_worker(self) -> None:
        """Asyncio task for replying to users with status of model deploymentevery N seconds."""

        # Set the frequency to reply to users.
        reply_freq_s = int(os.environ.get("COORDINATOR_PROCESSOR_REPLY_FREQ_S", "3"))

        while (
            self.status != ProcessorStatus.READY
            and self.status != ProcessorStatus.CANCELLED
        ):
            if self.status == ProcessorStatus.PROVISIONING:
                self.reply("Model Provisioning...")
            elif self.status == ProcessorStatus.DEPLOYING:
                self.reply("Model Deploying...")

            await asyncio.sleep(reply_freq_s)

    def reply(
        self,
        description: str | None = None,
        status: BackendResponseModel.JobStatus = BackendResponseModel.JobStatus.QUEUED,
    ) -> None:
        """
        Reply to all users with a message.

        Args:
            description (str | None, optional): The message to send to users.
                If None, a default queue position message is sent.
        """

        for i, request in enumerate(self.queue._queue):
            request.create_response(
                status,
                logger,
                (
                    description
                    if description is not None
                    else f"Moved to position {i + 1} in Queue."
                ),
            ).respond()

    def purge(self, message: Optional[str] = None):
        if message is None:
            message = "Critical server error occurred. Please try again later. Sorry for the inconvenience."

        self.reply(message, status=BackendResponseModel.JobStatus.ERROR)
