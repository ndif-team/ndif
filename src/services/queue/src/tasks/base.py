import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger("ndif")

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
        self._failed = False
        self._failure_reason: Optional[str] = None


    @property
    @abstractmethod
    def status(self):
        """
        Abstract property for the task status.
        
        Subclasses should implement their own status logic based on their specific
        requirements and backend integration.
        """
        pass


    def get_state(self) -> Dict[str, Any]:
        """
        Get the state of the task.
        
        Returns:
            Dictionary containing task state information
        """
        state = {
            "id": self.id,
            "position": self.position,
            "status": self.status,
            "retries": self.retries,
            "created_at": self.created_at.isoformat(),
        }
        
        if self._failed and self._failure_reason:
            state["failed"] = self._failure_reason
        
        return state


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


    def respond(self, description : Optional[str] = None) -> str:
        """
        Default implementation for responding to task updates.
        
        Subclasses can override this to provide specific response logic
        (e.g., sending updates to clients, logging, etc.).
        """

        if description:
            pass
        elif self.position is not None:
            description = f"{self.id} - Moved to position {self.position + 1}"
        else:
            description = f"{self.id} - Status updated to {self.status}"
        
        return description

    def respond_failure(self, description: Optional[str] = None) -> str:
        """
        Handle failure response for the task.
        
        This method sets the task failure state and provides a default
        failure response. Subclasses can override for specific failure handling.
        
        Args:
            description: Optional failure description
            
        Returns:
            The failure description used
        """
        if description is None:
            description = f"{self.id} - Task failed!"
        
        self._failed = True
        self._failure_reason = description
        
        logger.debug(f"Task {self.id} failed with description: {description}")
        
        return description

    def __str__(self):
        return f"{self.__class__.__name__}({self.id})"