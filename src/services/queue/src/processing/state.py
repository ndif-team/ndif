import enum

class ProcessorState(enum.Enum):
    """
    State of a processor.
    """
    UNINITIALIZED = "uninitialized"
    INACTIVE = "inactive"
    UNAVAILABLE = "unavailable"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    #TERMINATED = "terminated"