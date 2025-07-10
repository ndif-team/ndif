import enum

class TaskStatus(enum.Enum):
    """
    Status of a task.
    """
    QUEUED = "queued"
    PENDING = "pending"
    DISPATCHED = "dispatched"
    COMPLETED = "completed"
    FAILED = "failed"