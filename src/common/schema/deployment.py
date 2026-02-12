import os
from pydantic import BaseModel, Field, ConfigDict
import torch

class DeploymentConfig(BaseModel):
    """Number of CPUs to allocate."""
    num_cpus: int = 2

    """Padding factor for the computed amount of GPU/CPU memory."""
    padding_factor: float = 0.15

    """Device map for the deployment."""
    device_map: str = "auto"

    """Whether to deploy the model as dedicated."""
    dedicated: bool = False

    """Data type for the deployment."""
    dtype: str | torch.dtype = "bfloat16"

    """Execution timeout for the deployment."""
    execution_timeout_seconds: float = Field(
        default_factory=lambda: float(os.environ.get("NDIF_EXECUTION_TIMEOUT_SECONDS", "3600"))
    )

    """Whether to dispatch the deployment on spawn."""
    dispatch: bool = True

    # This was on BaseModelDeploymentArgs but unused, moved it here but commenting out for now 
    # model_config = ConfigDict(arbitrary_types_allowed=True)

    def __str__(self):
        return (
            "DeploymentConfig("
            f"num_cpus={self.num_cpus}, "
            f"padding_factor={self.padding_factor}, "
            f"device_map={self.device_map}, "
            f"dedicated={self.dedicated}, "
            f"dtype={self.dtype}, "
            f"execution_timeout_seconds={self.execution_timeout_seconds}, "
            f"dispatch={self.dispatch}"
            ")"
        )