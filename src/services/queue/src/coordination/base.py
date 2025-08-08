import asyncio
import logging
import multiprocessing
import os
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar

from ..processing.base import Processor
from ..processing.status import ProcessorStatus, DeploymentStatus
from .status import CoordinatorStatus

# Generic types for coordinators
T = TypeVar("T")  # Task type
P = TypeVar("P")  # Processor type

logger = logging.getLogger("ndif")

class Coordinator(ABC, Generic[T, P]):
    """
    Abstract base class for coordinators that manage processors.

    This class defines the core functionality for coordinating tasks across
    multiple processors, without making assumptions about the specific backend
    or implementation. Subclasses can implement specific backend integrations.
    """

    def __init__(
        self,
        tick_interval: float = 1.0,
        max_retries: int = 3,
        max_consecutive_errors: int = 5,
    ):
        self.active_processors: Dict[str, P] = {}
        self.inactive_processors: Dict[str, P] = {}
        self.max_retries = max_retries
        self.tick_interval = tick_interval
        self.tick_count = 0
        self.running = False
        self._thread = None
        self._error_count = 0
        self._max_consecutive_errors = max_consecutive_errors
        self._status = CoordinatorStatus.STOPPED  # Private status cache

        # Attributes accessible from outside of the coordinator thread (e.g. by the Queue app)
        manager = multiprocessing.Manager()
        self._in_tick_flag = manager.Value('b', False)
        self._sleep_event = manager.Event()
        self._backend_handle = None
        
    @property
    def in_tick(self) -> bool:
        """Returns True if currently processing a tick, False otherwise."""
        return self._in_tick_flag.value
    
    @in_tick.setter
    def in_tick(self, value: bool):
        """Sets whether we're currently processing a tick."""
        self._in_tick_flag.value = value
    
    @property
    def backend_handle(self):
        """
        The backend handle for coordinator operations.
        
        This provides an abstraction similar to Processor.handle, allowing
        coordinators to access their backend services (e.g., Ray controller,
        Kubernetes API, etc.). Subclasses should override this property
        to provide their specific backend handle.
        
        Returns:
            The backend handle or None if not available
        """
        return self._backend_handle
    
    @property
    def status(self):
        """
        The status of the coordinator.
        """
        if not self.running:
            self._status = CoordinatorStatus.STOPPED
        elif self._error_count >= self._max_consecutive_errors:
            self._status = CoordinatorStatus.ERROR
        else:
            self._status = CoordinatorStatus.RUNNING
            
        return self._status

    def get_state(self) -> Dict[str, Any]:
        """
        Get the state of the coordinator.

        Returns:
            Dictionary containing coordinator state information
        """
        return {
            "status": self._status,
            "active_processors": [
                processor.get_state() for processor in self.active_processors.values()
            ],
            "inactive_processors": [
                processor.get_state() for processor in self.inactive_processors.values()
            ],
            "error_count": self._error_count,
            "total_processors": len(self.active_processors)
            + len(self.inactive_processors),
            "tick_count": self.tick_count,
            "datetime": datetime.now(),
        }

    def start(self):
        """Start the coordinator."""
        if self.running:
            logger.warning("Coordinator is already running")
            return

        try:
            self._error_count = 0
            self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
            self.running = True
            self._thread.start()
            
            logger.info("Coordinator started successfully")
        except Exception as e:
            self._thread = None
            logger.exception(f"Failed to start coordinator: {e}")
            raise

    def stop(self):
        """Stop the coordinator."""
        if not self.running:
            logger.warning("Coordinator is not running")
            return

        try:

            # Clean up all processors
            self._cleanup_all_processors()
            self.running = False
            logger.info("Coordinator stopped successfully")
        except Exception as e:
            logger.exception(f"Error stopping coordinator: {e}")
            raise

    def remove_task(self, task_id: str) -> bool:
        """
        Remove a task from the coordinator by placing it on the deletion queue.
        
        Searches through all active processors to find the task and marks it for deletion.
        
        Args:
            task_id: The ID of the task to remove
            
        Returns:
            True if task was found and marked for deletion, False otherwise
        """
        for processor in self.active_processors.values():
            if processor.has_task(task_id):
                processor.deletion_queue.append(task_id)
                logger.debug(f"[COORDINATOR] Processor {processor.id} had task placed on deletion queue: {task_id}")
                return True

        logger.debug(f"[COORDINATOR] Task {task_id} attempted to be removed from queue but was not found in any active processors")
        return False

    def evict_processor(
        self, processor_key: str, user_message: Optional[str] = None, *args, **kwargs
    ) -> bool:
        """Evict the procesor for a given key. Returns True if the processor no longer exists in an active state after the operation."""

        if processor_key in self.active_processors:
            processor = self.active_processors[processor_key]
            evicted = self._evict(processor, user_message=user_message, *args, **kwargs)
            self.active_processors.pop(processor_key)
            self.inactive_processors[processor_key] = processor

        elif processor_key in self.inactive_processors:
            processor = self.inactive_processors[processor_key]
            evicted = self._evict(processor, user_message=user_message, *args, **kwargs)

        else:
            logger.warning(
                f"Warning: An eviction attempt was made for a processor which does not exist: {processor_key}"
            )
            evicted = True

        return evicted

    def _interrupt_sleep(self):
        """Interrupts the current sleep in the event loop."""
        self._sleep_event.set()

    @contextmanager
    def _tick_context(self):
        """Context manager to handle tick state and ensure cleanup."""
        self.in_tick = True
        try:
            yield
        finally:
            self.in_tick = False

    def _run_event_loop(self):
        """
        Run the coordinator's event loop.
        """
        while self.status == CoordinatorStatus.RUNNING:
            try:
                # Clear the event before waiting (in case it was set previously)
                self._sleep_event.clear()
                
                # Sleep for tick_interval or until interrupted
                interrupted = self._sleep_event.wait(self.tick_interval)
                
                if interrupted:
                    logger.debug("Sleep interrupted, processing tick immediately")
                
                with self._tick_context():
                    self._process_lifecycle_tick()
                    self.tick_count += 1
                    self._error_count = 0  # Reset error count on successful iteration
                    
            except asyncio.CancelledError:
                logger.info("Event loop cancelled")
                break
            except Exception as e:
                self._error_count += 1
                logger.exception(
                    f"Error in event loop (attempt {self._error_count}): {e}"
                )

                if self._error_count >= self._max_consecutive_errors:
                    logger.exception(
                        f"Too many consecutive errors ({self._error_count}), stopping coordinator"
                    )
                    self.stop()
                    break

    def _process_lifecycle_tick(self):
        """
        Advance the lifecycle of all active processors.
        """
        processors_to_deactivate = []
        processors_to_deploy = []
        failed_processors = []

        #logger.debug(f"[COORDINATOR] Processing lifecycle tick #{self.tick_count} with {len(self.active_processors)} active processors")

        for processor_key, processor in self.active_processors.items():
            try:
                # Modify state (except for actions which need to be done in batch and background tasks)
                processor.advance_lifecycle()
                processor_status = processor.status

                if processor_status == ProcessorStatus.UNINITIALIZED:
                    logger.info(f"[COORDINATOR] Processor {processor_key} needs deployment")
                    processors_to_deploy.append(processor)

                elif processor_status == ProcessorStatus.INACTIVE:
                    logger.info(f"[COORDINATOR] Processor {processor_key} became inactive")
                    processors_to_deactivate.append(processor_key)

                elif processor_status in [
                    ProcessorStatus.TERMINATED,
                    ProcessorStatus.UNAVAILABLE,
                ]:
                    logger.warning(f"[COORDINATOR] Processor {processor_key} failed with status: {processor_status}")
                    failed_processors.append(processor)

                elif processor_status == ProcessorStatus.PROVISIONING:

                    update_wait = float(
                        os.environ.get("_COORDINATOR_UPDATE_INTERVAL", 1)
                    )
                    ticks_per_update = max(1, int(update_wait // self.tick_interval))
                    if self.tick_count % ticks_per_update == 0:
                        try:
                            processor.notify_pending_task()
                        except Exception as e:
                            logger.exception(f"Failed to notify pending tasks: {e}")

            except Exception as e:
                logger.exception(
                    f"[COORDINATOR] Error advancing lifecycle for processor {processor_key}: {e}"
                )
                processors_to_deactivate.append(processor_key)

        # Deploy processors
        if processors_to_deploy:
            logger.info(f"[COORDINATOR] Deploying {len(processors_to_deploy)} processors")
            self._deploy(processors_to_deploy)

        # Move processors to inactive status
        for processor_key in processors_to_deactivate:
            self._deactivate_processor(processor_key)

        # Clean up failed processors
        for processor in failed_processors:
            self._handle_processor_failure(processor)

    def route_request(self, request: T) -> bool:
        """
        Route a request to the appropriate processor.

        Args:
            request: The request to route

        Returns:
            True if routing was successful, False otherwise
        """
        try:
            # Validate request and extract processor key
            processor_key = self._get_processor_key(request)
            if not request or not processor_key:
                logger.exception("Invalid request: missing request or processor key")
                return False

            # Try to route to existing active processor
            if processor_key in self.active_processors:
                processor = self.active_processors[processor_key]
                success = processor.enqueue(request)
                if success:
                    logger.debug(f"Request {request.id} routed to active processor {processor_key}")
                    return True
                else:
                    logger.exception(f"Failed to enqueue request {request.id} to active processor {processor_key}")
                    return False

            # Try to route to inactive processor
            elif processor_key in self.inactive_processors:
                processor = self.inactive_processors[processor_key]

                # Restart the processor to ensure clean state
                processor.restart()
                success = processor.enqueue(request)
                if success:
                    # Move processor to active state
                    self.active_processors[processor_key] = processor
                    del self.inactive_processors[processor_key]
                    logger.info(f"Activated processor {processor_key} and routed request {request.id}")
                    return True
                else:
                    logger.exception(f"Failed to enqueue request {request.id} to inactive processor {processor_key}")
                    return False

            # Create new processor
            else:
                try:
                    processor = self._create_processor(processor_key)
                    success = processor.enqueue(request)
                    if success:
                        self.active_processors[processor_key] = processor
                        logger.info(f"Created new processor {processor_key} and routed request {request.id}")
                        return True
                    else:
                        logger.exception(f"Failed to enqueue request {request.id} to new processor {processor_key}")
                        return False
                except Exception as e:
                    logger.exception(f"Failed to create processor {processor_key}: {e}")
                    return False

        except Exception as e:
            logger.exception(f"Error routing request {request.id if request else 'unknown'}: {e}")
            return False

        finally:
            # Force next tick to start immediately
            self._interrupt_sleep()

    def _deploy(self, processors: List[P]):
        """
        Deploy processors using the backend handle.
        
        Default implementation sets all processors to 'deployed' status.
        Subclasses can override this for complex deployment protocols that
        use the backend_handle (e.g., Ray, Kubernetes, etc.).
        
        Args:
            processors: List of processors to deploy
        """
        if not processors:
            return
            
        # If no backend handle, use simple default deployment
        if self.backend_handle is None:
            for processor in processors:
                processor.backend_status = DeploymentStatus.DEPLOYED
                logger.info(f"[COORDINATOR] Processor {processor.id} deployed (default implementation)")
            logger.debug(f"[COORDINATOR] Deployed {len(processors)} processors using default implementation")
        else:
            # Backend handle is available - subclasses should override for specific protocols
            logger.warning("[COORDINATOR] Backend handle available but _deploy not overridden - using default deployment")
            for processor in processors:
                processor.backend_status = DeploymentStatus.DEPLOYED
                logger.info(f"[COORDINATOR] Processor {processor.id} deployed (default with backend handle)")
            logger.debug(f"[COORDINATOR] Deployed {len(processors)} processors using default implementation with backend handle")

    def _evict(self, processor: P, user_message: str = None) -> bool:
        """
        Concrete implementation of processor eviction.
        
        This method handles common eviction logic:
        - Sets termination flag
        - Notifies dispatched task with failure message
        - Notifies all queued tasks with failure message
        - Clears processor queue
        
        Subclasses can override _get_eviction_message() to customize messages.
        
        Args:
            processor: The processor to evict
            user_message: Optional user-facing message explaining the eviction
            
        Returns:
            True if eviction was successful
        """
        # Set termination flag before restart to maintain eviction semantics
        processor._has_been_terminated = True

        # If a user has a job running on this processor at eviction, inform them with a detailed reason
        if processor.dispatched_task:
            status_message = self._get_eviction_message("execution")
            full_message = f"{status_message} {user_message}" if user_message else status_message
            processor.dispatched_task.respond_failure(full_message)
            processor.dispatched_task = None

        # Inform all queued tasks that their requests could not be processed due to eviction
        for task in processor.queue:
            status_message = self._get_eviction_message("queue")
            full_message = f"{status_message} {user_message}" if user_message else status_message
            task.respond_failure(description=full_message)

        processor.queue[:] = []  # Clear queue (ListProxy compatible)
        return True
 

    def _deactivate_processor(self, processor_key: str):
        """
        Move a processor from active to inactive state.

        Args:
            processor_key: The key of the processor to deactivate
        """
        try:
            if processor_key in self.active_processors:
                processor = self.active_processors.pop(processor_key)
                if processor_key in self.inactive_processors:
                    logger.warning(
                        f"Processor {processor_key} is already in inactive state. This should not happen."
                    )
                self.inactive_processors[processor_key] = processor
                logger.info(f"[COORDINATOR] Deactivated processor {processor_key}")
        except Exception as e:
            logger.exception(f"[COORDINATOR] Error deactivating processor {processor_key}: {e}")

    def _cleanup_all_processors(self):
        """
        Clean up all processors during shutdown.
        """
        try:
            # Clear all processors
            self.active_processors.clear()
            self.inactive_processors.clear()
            logger.info("[COORDINATOR] Cleaned up all processors")
        except Exception as e:
            logger.exception(f"[COORDINATOR] Error during final cleanup: {e}")

    @abstractmethod
    def _get_processor_key(self, request: T) -> str:
        """
        Extract the processor key from a request.
        
        Args:
            request: The request to extract key from
            
        Returns:
            The processor key for this request
        """
        pass

    def _handle_processor_failure(self, processor: P):
        """
        Handle a failed processor by generating appropriate user message and evicting it.
        
        This method handles the common pattern:
        1. Determine failure reason based on processor status
        2. Generate user-facing message via _get_failure_message hook
        3. Evict the processor with the message
        
        Subclasses should override _get_failure_message() to provide backend-specific messages.
        
        Args:
            processor: The failed processor to handle
        """
        processor_status = processor.status
        user_message = self._get_failure_message(processor, processor_status)
        
        # Notify all queued requests of the failure
        self.evict_processor(processor.id, user_message=user_message)
    
    @abstractmethod
    def _get_failure_message(self, processor: P, status) -> str:
        """
        Generate a user-facing failure message based on processor and its status.
        
        Subclasses should implement this to provide backend-specific failure explanations.
        
        Args:
            processor: The failed processor
            status: The processor's current status
            
        Returns:
            A user-facing message explaining why the processor failed
        """
        pass

    def _create_processor(self, processor_key: str) -> P:
        """
        Create a new processor.
        
        Default implementation creates a base Processor. Subclasses should
        override this to create their specific processor types.

        Args:
            processor_key: The key for the processor

        Returns:
            The created processor
        """
        return Processor(processor_key, max_retries=self.max_retries)

        
    def _get_eviction_message(self, context: str) -> str:
        """
        Get context-specific eviction message.
        
        Args:
            context: Either "execution" or "queue" indicating where the task was
            
        Returns:
            A status message describing what happened to the task
        """
        if context == "execution":
            return "Task interrupted during execution."
        elif context == "queue":
            return "Task removed from queue."
        else:
            return "Task interrupted."