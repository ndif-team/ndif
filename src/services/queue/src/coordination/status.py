from enum import Enum

class CoordinatorStatus(Enum):
    """
    Base coordinator statuses.
    """
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"

