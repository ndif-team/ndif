from __future__ import annotations

from typing import Dict, List, Optional, Union

from pydantic import BaseModel

from ..types import MODEL_KEY


class DeploymentConfig(BaseModel):
    """Configuration for a model deployment."""

    dedicated: bool = False
    padding_factor: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None

    @staticmethod
    def normalize(
        deployments: Union[
            MODEL_KEY,
            List[MODEL_KEY],
            Dict[MODEL_KEY, "DeploymentConfig"],
        ],
    ) -> Dict[MODEL_KEY, "DeploymentConfig"]:
        """Normalize various input formats into a dict of model_key -> DeploymentConfig.

        Accepts:
            - A single model key string
            - A list of model key strings
            - A dict mapping model key strings to DeploymentConfig instances

        Returns:
            A dict mapping model keys to DeploymentConfig instances.
        """
        if isinstance(deployments, dict):
            return deployments

        if isinstance(deployments, str):
            deployments = [deployments]

        return {key: DeploymentConfig() for key in deployments}
