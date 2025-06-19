import enum

class ProcessorState(enum.Enum):
    """
    State of a processor.
    """
    INACTIVE = "inactive"
    ACTIVE = "active"
    DISCONNECTED = "disconnected"
