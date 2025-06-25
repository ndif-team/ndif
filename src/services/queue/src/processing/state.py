from enum import Enum

class ProcessorState(Enum):
    """
    State of a processor.
    """
    UNINITIALIZED = "uninitialized"
    INACTIVE = "inactive"
    UNAVAILABLE = "unavailable"
    PROVISIONING = "provisioning"
    ACTIVE = "active"
    TERMINATED = "terminated"

class DeploymentState(Enum):
    """
    State of deployment returned from controller (CandidateLevel).
    """
    UNINITIALIZED = "uninitialized"
    DEPLOYED = "deployed"
    CACHED_AND_FREE = "cached_and_free"
    FREE = "free"
    CACHED_AND_FULL = "cached_and_full"
    FULL = "full"
    CANT_ACCOMMODATE = "cant_accommodate"