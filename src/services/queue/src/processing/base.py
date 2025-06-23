from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Generic, TypeVar
from datetime import datetime
from ..tasks.base import Task

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

    def state(self) -> Dict[str, Any]:
        """
        Get the state of the processor.
        
        Returns:
            Dictionary containing processor state information
        """
        return {
            "status": self.status,
            "dispatched_task": self.dispatched_task.state() if self.dispatched_task else None,
            "queue": [task.state() for task in self.queue],
            "last_dispatched": self.last_dispatched,
        }

    def advance_lifecycle(self) -> bool:
        """
        Check whether the processor state needs to be updated.
        
        This method implements the core lifecycle logic that should work
        for any task processor implementation.
        
        Returns:
            True if the state was updated, False otherwise
        """
        # Get current status (this may trigger state updates in subclasses)
        current_status = self.status
        
        # Handle inactive state
        if self._is_inactive(current_status):
            return False

        # Handle disconnected state
        if self._is_disconnected(current_status):
            return False

        # If no task is currently dispatched, try to dequeue one
        if not self.dispatched_task:
            self.dispatched_task = self.dequeue()
            if not self.dispatched_task:
                return False

        # Handle different task states
        task_status = self.dispatched_task.status
        
        if task_status == self._get_queued_state():
            self._log_error(f"Dispatched task is in queued state, this should not happen")
            self._dispatch()
            return True

        if task_status == self._get_pending_state():
            self._dispatch()
            return True

        if task_status == self._get_dispatched_state():
            # Task is already dispatched, no action needed
            return False

        if task_status == self._get_completed_state():
            self._handle_completed_dispatch()
            return True

        if task_status == self._get_failed_state():
            self._handle_failed_dispatch()
            return True

        self._log_warning(f"Processor is in an unexpected state: {current_status}")
        return False

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

    # Abstract methods for status checking - subclasses can override these
    # to provide their own status logic
    
    def _is_inactive(self, status) -> bool:
        """
        Check if the processor is inactive.
        
        Args:
            status: The current status
            
        Returns:
            True if inactive, False otherwise
        """
        return False  # Default implementation, subclasses can override

    def _is_disconnected(self, status) -> bool:
        """
        Check if the processor is disconnected.
        
        Args:
            status: The current status
            
        Returns:
            True if disconnected, False otherwise
        """
        return False  # Default implementation, subclasses can override

    # Abstract methods for task states - subclasses should implement these
    # to provide their own state constants
    
    @abstractmethod
    def _get_queued_state(self):
        """Return the queued state constant."""
        pass

    @abstractmethod
    def _get_pending_state(self):
        """Return the pending state constant."""
        pass

    @abstractmethod
    def _get_dispatched_state(self):
        """Return the dispatched state constant."""
        pass

    @abstractmethod
    def _get_completed_state(self):
        """Return the completed state constant."""
        pass

    @abstractmethod
    def _get_failed_state(self):
        """Return the failed state constant."""
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
