import logging
from abc import ABC
from typing import Dict, Any, List, Optional, Generic, TypeVar
from datetime import datetime
from ..tasks.base import Task
from ..tasks.status import TaskStatus
from .status import ProcessorStatus, DeploymentStatus
# Generic type for tasks
T = TypeVar('T', bound=Task)

logger = logging.getLogger("ndif")

class Processor(ABC, Generic[T]):
    """
    Abstract base class for task processors.
    
    This class defines the core functionality for processing tasks in a queue,
    without making assumptions about the specific backend or queue implementation.
    Subclasses can implement specific backend integrations (e.g., Ray, local processing).
    """
    

    def __init__(self, processor_id: str, max_retries: int = 3, max_tasks: int = None):
        self.id = processor_id
        self.max_retries = max_retries
        self.max_tasks = max_tasks
        self.last_dispatched: Optional[datetime] = None
        self.dispatched_task: Optional[T] = None
        self.deletion_queue: List[str] = []  # Track IDs to delete
        self._has_been_terminated: bool = False
        self.backend_status: Optional[Any] = None
        self._queue: List[T] = []
        self._status = None  # Private status cache


    @property
    def queue(self) -> List[T]:
        """
        The task queue.
        
        Subclasses can override this to provide their specific queue implementation.
        For example, RequestProcessor uses Manager().list() for multiprocessing.
        """
        return self._queue


    @property
    def handle(self):
        """
        The backend handle for task execution.
        
        Subclasses can override this to provide their specific backend handle
        (e.g., Ray app handle, HTTP client, local function reference).
        Defaults to None - can be set externally or overridden by subclasses.
        """
        return getattr(self, '_handle', None)


    @property
    def status(self):
        """
        The status of the processor based on backend state and queue activity.
        """
        
        # Check termination first
        if self._has_been_terminated:
            self._status = ProcessorStatus.TERMINATED
        
        # Check if processor is inactive (no work)
        elif not self.dispatched_task and len(self.queue) == 0:
            self._status = ProcessorStatus.INACTIVE
            
        # Check if draining (at capacity)
        elif self.max_tasks and len(self.queue) >= self.max_tasks:
            self._status = ProcessorStatus.DRAINING
            
        # Backend-specific status checks
        elif self.backend_status == DeploymentStatus.CANT_ACCOMMODATE:
            self._status = ProcessorStatus.UNAVAILABLE
            
        # Check if we're provisioning (scheduled but no handle yet)
        elif hasattr(DeploymentStatus, 'FREE'):  # Check if we're dealing with Ray statuses
            scheduled_statuses = [
                DeploymentStatus.FREE,
                DeploymentStatus.FULL,
                DeploymentStatus.CACHED_AND_FREE,
                DeploymentStatus.CACHED_AND_FULL,
            ]
            if self.backend_status in scheduled_statuses and self.handle is None:
                self._status = ProcessorStatus.PROVISIONING
            # If we have a handle or are deployed, we're active
            elif self.handle is not None or self.backend_status == getattr(DeploymentStatus, 'DEPLOYED', None):
                self._status = ProcessorStatus.ACTIVE
            # Default to uninitialized if no handle yet
            else:
                self._status = ProcessorStatus.UNINITIALIZED
        
        # If we have a handle or are deployed, we're active
        elif self.handle is not None or self.backend_status == getattr(DeploymentStatus, 'DEPLOYED', None):
            self._status = ProcessorStatus.ACTIVE
            
        # Default to uninitialized if no handle yet
        else:
            self._status = ProcessorStatus.UNINITIALIZED
            
        return self._status


    def get_state(self) -> Dict[str, Any]:
        """
        Get the state of the processor.
        
        Returns:
            Dictionary containing processor state information
        """
        return {
            "id": self.id,
            "status": self._status,
            "dispatched_task": self.dispatched_task.get_state() if self.dispatched_task else None,
            "queue": [task.get_state() for task in self.queue],
            "last_dispatched": self.last_dispatched,
            "deletion_queue": self.deletion_queue,
            "has_been_terminated": self._has_been_terminated,
            "backend_status": self.backend_status,
        }

    def has_task(self, task_id: str) -> bool:
        """Return whether the processor contains a task with the given id."""
        if self.dispatched_task and getattr(self.dispatched_task, 'id', None) == task_id:
            return True
        for task in self.queue:
            if getattr(task, 'id', None) == task_id:
                return True
        return False

    def notify_pending_task(self, description: Optional[str] = None):
        """Notify all pending tasks in the queue with a description."""
        if description is None:
            description = "A task is pending... stand by."
        for pending_task in self.queue:
            pending_task.respond(description)

    def enqueue(self, task: T) -> bool:
        """
        Enqueue a task and handle both success and failure responses.
        
        Args:
            task: The task to enqueue
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if not self.max_tasks or len(self.queue) < self.max_tasks:
                position = len(self.queue)
                task.position = position  # Set the position on the task
                self.queue.append(task)
                
                # Handle success response
                try:
                    task.respond(
                        description=f"Task {getattr(task, 'id', 'unknown')} has been added to the queue. Currently at position {position + 1}"
                    )
                except Exception as e:
                    logger.error(f"Error responding to task {getattr(task, 'id', 'unknown')} at queued stage: {e}")
                
                return True
            else:
                # Handle failure response for capacity limit
                if self.status == ProcessorStatus.DRAINING:
                    task.respond_failure(
                        f"Queue has currently at max capacity of {self.max_tasks}, please try again later."
                    )
                else:
                    task.respond_failure(
                        f"Task could not be enqueued for unknown reason."
                    )
                return False
        except Exception as e:
            logger.exception(f"Error enqueuing task {getattr(task, 'id', 'unknown')}: {e}")
            return False


    def dequeue(self) -> Optional[T]:
        """
        Dequeue a task.
        
        Returns:
            The dequeued task, or None if queue is empty
        """
        if len(self.queue) == 0:
            return None
        
        task = self.queue.pop(0)
        task.position = None
        self.update_positions()
        logger.debug(f"Dequeued task {getattr(task, 'id', 'unknown')}")
        return task

    
    def advance_lifecycle(self) -> bool:
        """
        Advance the lifecycle of the processor.
        """
        self.process_deletion_queue()
        return self._advance_lifecycle_core()

    def _advance_lifecycle_core(self) -> bool:
        current_status = self.status
        if self._is_invariant_status(current_status):
            return False

        # If no task is currently dispatched, try to dequeue one
        if not self.dispatched_task:
            self.dispatched_task = self.dequeue()
            if not self.dispatched_task:
                return False
        task_status = self.dispatched_task.status
        if task_status == TaskStatus.QUEUED:
            logger.error("Dispatched task is in queued status, this should not happen")
            self._dispatch()
            return True
        if task_status == TaskStatus.PENDING:
            self._dispatch()
            return True
        if task_status == TaskStatus.DISPATCHED:
            # Task is already dispatched, no action needed
            return False
        if task_status == TaskStatus.COMPLETED:
            self._handle_completed_dispatch()
            return True
        if task_status == TaskStatus.FAILED:
            self._handle_failed_dispatch()
            return True
        logger.warning(f"Processor is in an unexpected status: {current_status}")
        return False

    def process_deletion_queue(self):
        """Process requests marked for deletion from the queue. Calls backend-specific cancel if needed."""
        if not self.deletion_queue:
            return
        deletion_ids = set(self.deletion_queue)
        i = 0
        while i < len(self.queue):
            if getattr(self.queue[i], 'id', None) in deletion_ids:
                deleted_task = self.queue.pop(i)
                logger.debug(f"[PROCESSOR] Deleted task {getattr(deleted_task, 'id', 'unknown')} from queue (position {i+1})")
            else:
                i += 1
        if self.dispatched_task and getattr(self.dispatched_task, 'id', None) in deletion_ids:
            self._cancel_dispatched_task()
        self.deletion_queue.clear()
        self.update_positions()

    def _cancel_dispatched_task(self):
        """
        Cancel the currently dispatched task and perform cleanup.
        
        This method handles common cleanup logic that applies regardless of backend:
        - Clears the deletion queue
        - Updates positions for remaining tasks in queue
        
        Subclasses should override this method to add backend-specific cancellation
        logic, but should call super()._cancel_dispatched_task() for cleanup.
        """
        # Clear the deletion queue
        self.deletion_queue.clear()
        
        # Update positions for remaining tasks
        self.update_positions()

        self.dispatched_task = None

    def update_positions(self, indices: Optional[List[int]] = None):
        """
        Update the positions of tasks in the queue.
        
        Args:
            indices: Optional list of indices to update. If None, updates all.
        """
        indices = indices or range(len(self.queue))
        for i in indices:
            if i < len(self.queue):
                task = self.queue[i]
                old_position = getattr(task, "position", None)
                if old_position != i:
                    if old_position is not None and abs(old_position - i) > 1:
                        logger.warning(
                            f"Task {getattr(task, 'id', 'unknown')} position changed from {old_position} to {i} (diff={i - old_position})"
                        )
                    task.position = i
                    task.respond()


    def _dispatch(self) -> bool:
        """
        Dispatch a task using the backend handle.
        
        This method provides common dispatch logic with error handling.
        Subclasses can override this method for backend-specific dispatch behavior.
        
        Returns:
            True if dispatch was successful, False otherwise
        """
        logger.debug(f"Attempting to dispatch task {getattr(self.dispatched_task, 'id', 'unknown')} on {self.id}")
        
        try:
            # Notify about dispatch attempt
            self.dispatched_task.respond(description="Dispatching task...")
        except Exception as e:
            logger.exception(f"Failed to respond to user about task being dispatched: {e}")
        
        try:
            # Use the task's run method with our handle
            success = self.dispatched_task.run(self.handle)
            if success:
                logger.debug(f"Successfully dispatched task {getattr(self.dispatched_task, 'id', 'unknown')} on {self.id}")
                self.last_dispatched = datetime.now()
            return success
        except Exception as e:
            logger.exception(f"Error during task dispatch on {self.id}: {e}")
            return False


    def _handle_completed_dispatch(self):
        """
        Handle a completed task.
        """
        logger.info(f"[TASK:{self.dispatched_task.id}] completed!")
        self.dispatched_task = None


    def _handle_failed_dispatch(self):
        """
        Handle a failed task with retry logic and failure notifications.
        """
        if self.dispatched_task.retries < self.max_retries:
            # Try again
            logger.exception(f"Task {self.dispatched_task.id} failed, retrying... (attempt {self.dispatched_task.retries + 1} of {self.max_retries})")
            self._dispatch()
            self.dispatched_task.retries += 1
        else:
            try:
                # Get backend-specific failure message
                failure_message = self._get_failure_message()
                self.dispatched_task.respond_failure(description=failure_message)
            except Exception as e:
                logger.exception(f"Error handling failed task {self.dispatched_task.id}: {e}")
            
            self.dispatched_task = None

    def _get_failure_message(self) -> str:
        """
        Get a failure message for when a task fails after maximum retries.
        
        Subclasses can override this method to provide backend-specific
        failure messages with more context about what went wrong.
        
        Returns:
            A string describing why the task failed
        """
        return "Task failed after maximum retries."

    def _is_invariant_status(self, current_status : ProcessorStatus) -> bool:
        """Return True if self.status is in a status invariant with respect to the current lifecycle"""
        invariant_statuses = [
            ProcessorStatus.UNINITIALIZED,
            ProcessorStatus.INACTIVE,
            ProcessorStatus.PROVISIONING,
            ProcessorStatus.UNAVAILABLE,
        ]
        return any(current_status == status for status in invariant_statuses)

    def __str__(self):
        return f"{self.__class__.__name__}({self.id})"

    def restart(self):
        """
        Restart the processor, resetting it to initial state.
        
        This method resets the processor to a clean state by:
        - Clearing the dispatched task
        - Clearing the deletion queue
        - Clearing the task queue
        - Resetting termination flag
        - Calling subclass-specific restart logic
        """
        logger.info(f"Restarting processor {self.id}")
        self.dispatched_task = None
        self.deletion_queue.clear()
        self._queue = []
        self._has_been_terminated = False
        self._restart_implementation()
        
    def _restart_implementation(self):
        """
        Hook for subclass-specific restart logic.
        
        Subclasses can override this method to handle their specific
        restart requirements (e.g., resetting deployment status, app handles, etc.).
        Default implementation does nothing.
        """
        pass