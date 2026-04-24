from __future__ import annotations

from typing import Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict

from ..types import MODEL_KEY


class DeploymentConfig(BaseModel):
    """Configuration for a model deployment."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    dedicated: bool = False
    padding_factor: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None
    actor_class: Optional[Union[str, type]] = None
    """Ray actor class used to serve this deployment.

    Accepts either a dotted import path (e.g. ``"ndif.services.ray.deployments.modeling.base.ModelActor"``)
    resolvable inside the Ray actor's Python path, or a class object already
    decorated with ``@ray.remote``. ``None`` defers to the controller's
    configured ``default_actor_class`` (set via ``NDIF_DEFAULT_ACTOR_CLASS``;
    defaults to ``ModelActor``).

    A user-supplied class must already be ``@ray.remote``-decorated — it will
    be called as ``actor_class.options(...).remote(...)``; we do not wrap it,
    so any ``num_cpus`` / ``num_gpus`` / ``max_restarts`` on the decorator are
    preserved as-is.
    """

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
