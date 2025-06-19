from enum import Enum

class CoordinatorState(Enum):
    """
    Base coordinator states.
    """
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"

