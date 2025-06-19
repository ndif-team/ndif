import enum

class TaskState(enum.Enum):
    """
    State of a task.
    """
    QUEUED = "queued"
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"