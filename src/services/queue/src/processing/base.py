from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Generic, TypeVar
from datetime import datetime
from ..tasks.base import Task
from ..tasks.status import TaskStatus

# Generic type for tasks
T = TypeVar('T', bound=Task)

class Processor(ABC, Generic[T]):
    """
    Abstract base class for task processors.
    
    This class defines the core functionality for processing tasks in a queue,
    without making assumptions about the specific backend or queue implementation.
    Subclasses can implement specific backend integrations (e.g., Ray, local processing).
    """
    

    def __init__(self, max_retries: int = 3):
        self.max_retries = max_retries
        self.last_dispatched: Optional[datetime] = None
        self.dispatched_task: Optional[T] = None
        self._needs_update = False


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
        }


    def enqueue(self, task: T) -> bool:
        """
        Enqueue a task.
        
        Args:
            task: The task to enqueue
            
        Returns:
            True if successful, False otherwise
        """
        try:
            self.queue.append(task)
            return True
        except Exception as e:
            self._log_error(f"Error enqueuing task: {e}")
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
        task.update_position(None)
        self._needs_update = True
        self._log_debug(f"Dequeued task {getattr(task, 'id', 'unknown')}")
        return task

    
    def advance_lifecycle(self) -> bool:
        """
        Check whether the processor "state" needs to be updated.
        
        This method implements the core lifecycle logic that should work
        for any task processor implementation.
        
        Returns:
            True if the state was updated, False otherwise
        """
        # Get current status (this may trigger status updates in subclasses)
        current_status = self.status

        if self._is_invariant_status(current_status):
            return False

        # If no task is currently dispatched, try to dequeue one
        if not self.dispatched_task:
            self.dispatched_task = self.dequeue()
            if not self.dispatched_task:
                return False

        # Handle different task statuses
        task_status = self.dispatched_task.status
        
        if task_status == TaskStatus.QUEUED:
            self._log_error(f"Dispatched task is in queued status, this should not happen")
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

        self._log_warning(f"Processor is in an unexpected status: {current_status}")
        return False


    def update_positions(self, indices: Optional[List[int]] = None):
        """
        Update the positions of tasks in the queue.
        
        Args:
            indices: Optional list of indices to update. If None, updates all.
        """
        indices = indices or range(len(self.queue))
        for i in indices:
            if i < len(self.queue):
                self._update_position(i)
        self._needs_update = False


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
        self.dispatched_task = None


    def _handle_failed_dispatch(self):
        """
        Handle a failed task.
        """
        if self.dispatched_task.retries < self.max_retries:
            # Try again
            self._log_error(f"Task failed, retrying... (attempt {self.dispatched_task.retries + 1} of {self.max_retries})")
            self._dispatch()
            self.dispatched_task.retries += 1
        else:
            try:
                # Try to inform about the failure
                self.dispatched_task.respond()
            except Exception as e:
                self._log_error(f"Error handling failed task: {e}")
            
            self.dispatched_task = None


    def _update_position(self, position: int):
        """
        Update the position of a task.
        """
        task = self.queue[position]
        task.update_position(position).respond()


    @abstractmethod
    def _is_invariant_status(self) -> bool:
        """Return True if self.status is in a status invariant with respect to the current lifecycle"""
        pass

    # Logging methods - subclasses can override these to use their own logging
    
    def _log_debug(self, message: str):
        """Log a debug message."""
        print(f"[DEBUG] {message}")


    def _log_error(self, message: str):
        """Log an error message."""
        print(f"[ERROR] {message}")


    def _log_warning(self, message: str):
        """Log a warning message."""
        print(f"[WARNING] {message}")


    def _log_info(self, message: str):
        """Log an info message."""
        print(f"[INFO] {message}")