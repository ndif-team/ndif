import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Generic, TypeVar
from datetime import datetime
from ..tasks.base import Task
from ..tasks.status import TaskStatus
from .status import ProcessorStatus
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
    

    def __init__(self, max_retries: int = 3, max_tasks: int = None):
        self.max_retries = max_retries
        self.max_tasks = max_tasks
        self.last_dispatched: Optional[datetime] = None
        self.dispatched_task: Optional[T] = None
        self.deletion_queue: List[str] = []  # Track IDs to delete


    @property
    @abstractmethod
    def queue(self) -> List[T]:
        """
        Abstract property for the task queue.
        
        Subclasses should override this to provide their specific queue implementation.
        For example, RequestProcessor might use Manager().list() for multiprocessing,
        while a local processor might use a simple list.
        """
        pass


    @property
    @abstractmethod
    def status(self):
        """
        Abstract property for the processor status.
        
        Subclasses should implement their own status logic based on their specific
        requirements (e.g., connected/disconnected for remote backends).
        """
        pass


    def get_state(self) -> Dict[str, Any]:
        """
        Get the state of the processor.
        
        Returns:
            Dictionary containing processor state information
        """
        return {
            "status": self.status,
            "dispatched_task": self.dispatched_task.get_state() if self.dispatched_task else None,
            "queue": [task.get_state() for task in self.queue],
            "last_dispatched": self.last_dispatched,
            "deletion_queue": self.deletion_queue,
        }

    def has_request(self, request_id: str) -> bool:
        """Return whether the processor contains a request with the given id."""
        if self.dispatched_task and getattr(self.dispatched_task, 'id', None) == request_id:
            return True
        for task in self.queue:
            if getattr(task, 'id', None) == request_id:
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
        Enqueue a task.
        
        Args:
            task: The task to enqueue
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if not self.max_tasks or len(self.queue) < self.max_tasks:
                self.queue.append(task)
                return True
            else:
                return False
        except Exception as e:
            logger.exception(f"Error enqueuing task: {e}")
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
            self.dispatched_task = None
        self.deletion_queue.clear()
        self.update_positions()

    def _cancel_dispatched_task(self):
        """Hook for subclasses to implement backend-specific cancellation logic."""
        pass

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


    @abstractmethod
    def _dispatch(self) -> bool:
        """
        Abstract method to dispatch a task.
        
        Subclasses should implement the specific dispatch logic for their backend.
        
        Returns:
            True if dispatch was successful, False otherwise
        """
        pass


    def _handle_completed_dispatch(self):
        """
        Handle a completed task.
        """
        logger.info(f"[TASK:{self.dispatched_task.id}] completed!")
        self.dispatched_task = None


    def _handle_failed_dispatch(self):
        """
        Handle a failed task.
        """
        if self.dispatched_task.retries < self.max_retries:
            # Try again
            logger.exception(f"Task failed, retrying... (attempt {self.dispatched_task.retries + 1} of {self.max_retries})")
            self._dispatch()
            self.dispatched_task.retries += 1
        else:
            try:
                # Try to inform about the failure
                self.dispatched_task.respond(description="Task failed after maximum retries.")
            except Exception as e:
                logger.exception(f"Error handling failed task: {e}")
            
            self.dispatched_task = None

    def _is_invariant_status(self, current_status : ProcessorStatus) -> bool:
        """Return True if self.status is in a status invariant with respect to the current lifecycle"""
        invariant_statuses = [
            ProcessorStatus.UNINITIALIZED,
            ProcessorStatus.INACTIVE,
            ProcessorStatus.PROVISIONING,
            ProcessorStatus.UNAVAILABLE,
        ]
        return any(current_status == status for status in invariant_statuses)

    def restart(self):
        """
        Restart the processor, resetting it to initial state.
        
        This method resets the processor to a clean state by:
        - Clearing the dispatched task
        - Clearing the deletion queue
        - Calling subclass-specific restart logic
        """
        logger.info("Restarting processor")
        self.dispatched_task = None
        self.deletion_queue.clear()
        self._restart_implementation()
        
    @abstractmethod
    def _restart_implementation(self):
        """
        Abstract method for subclass-specific restart logic.
        
        Subclasses should implement this method to handle their specific
        restart requirements (e.g., resetting deployment status, app handles, etc.).
        """
        pass