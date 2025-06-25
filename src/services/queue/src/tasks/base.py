from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime

class Task(ABC):
    """
    Abstract base class for tasks that can be processed by a Processor.
    
    This class defines the core functionality for tasks in a queue,
    without making assumptions about the specific backend or implementation.
    Subclasses can implement specific backend integrations (e.g., Ray, local processing).
    """
    
    def __init__(self, task_id: str, data: Any, position: Optional[int] = None):
        self.id = task_id
        self.data = data
        self.position = position
        self.retries = 0
        self.created_at = datetime.now()

    @property
    @abstractmethod
    def status(self):
        """
        Abstract property for the task status.
        
        Subclasses should implement their own status logic based on their specific
        requirements and backend integration.
        """
        pass

    def state(self) -> Dict[str, Any]:
        """
        Get the state of the task.
        
        Returns:
            Dictionary containing task state information
        """
        return {
            "id": self.id,
            "position": self.position,
            "status": self.status,
            "retries": self.retries,
            "created_at": self.created_at.isoformat(),
        }

    def update_position(self, position: int):
        """
        Update the position of the task in the queue.
        
        Args:
            position: The new position
            
        Returns:
            Self for method chaining
        """
        self.position = position
        return self

    @abstractmethod
    def run(self, backend_handle) -> bool:
        """
        Abstract method to run the task.
        
        Args:
            backend_handle: The backend handle to use for execution
                           (e.g., Ray app_handle, local function, HTTP client)
            
        Returns:
            True if the task was successfully started, False otherwise
        """
        pass

    def respond(self) -> str:
        """
        Default implementation for responding to task updates.
        
        Subclasses can override this to provide specific response logic
        (e.g., sending updates to clients, logging, etc.).
        """
        if self.position is not None:
            description = f"{self.id} - Moved to position {self.position + 1}"
        else:
            description = f"{self.id} - Status updated to {self.status}"
        
        self._log_debug(description)

        return description

    def increment_retries(self) -> int:
        """
        Increment the retry counter and return the new count.
        
        Returns:
            The new retry count
        """
        self.retries += 1
        return self.retries

    def reset_retries(self) -> int:
        """
        Reset the retry counter to 0.
        
        Returns:
            The reset retry count (0)
        """
        self.retries = 0
        return self.retries

    def is_retryable(self, max_retries: int) -> bool:
        """
        Check if the task can be retried.
        
        Args:
            max_retries: Maximum number of retries allowed
            
        Returns:
            True if the task can be retried, False otherwise
        """
        return self.retries < max_retries

    # Logging methods - subclasses can override these to use their own logging
    
    def _log_debug(self, message: str):
        """Log a debug message."""
        print(f"[DEBUG] Task {self.id}: {message}")

    def _log_error(self, message: str):
        """Log an error message."""
        print(f"[ERROR] Task {self.id}: {message}")

    def _log_warning(self, message: str):
        """Log a warning message."""
        print(f"[WARNING] Task {self.id}: {message}")

    def _log_info(self, message: str):
        """Log an info message."""
        print(f"[INFO] Task {self.id}: {message}")
