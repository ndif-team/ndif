"""Data contracts for the Controller / Cluster RPCs (deploy, evict, get_deployment).

These live in common/ because both the queue (API side) and the controller
(Ray side) speak them. The queue is written against the deploy / get_deployment
shapes; the controller produces the full replica/deploy state.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field

from ..types import MODEL_KEY, REPLICA_ID


# ---------- deploy ----------------------------------------------------------


class DeploymentConfig(BaseModel):
    """Configuration for deploying a model.

    ``replicas`` is **additive**: every deploy places that many *new* replicas
    regardless of what's already running (use evict to shrink). ``pinned``
    marks a deployment exempt from autoscaling/cache eviction.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    pinned: bool = False
    replicas: int = 1
    # Whether this deployment may run the model's own repo code (HF
    # trust_remote_code). Inherited from the trusted flag of the request that
    # kicked off the deployment; the controller threads it into the evaluator's
    # size estimate and the actor's model load, which must agree.
    trusted: bool = False
    padding_factor: Optional[float] = None
    execution_timeout_seconds: Optional[float] = None
    # torch dtype name (e.g. "bfloat16") to load the model in and estimate its
    # size with. None -> the controller's default_dtype.
    dtype: Optional[str] = None
    actor_class: Optional[Union[str, type]] = None
    """Ray actor class serving this deployment: a dotted import path resolvable
    inside the Ray actor's Python path, or an already-``@ray.remote`` class.
    ``None`` defers to the controller's ``default_model_actor_class``."""

    @staticmethod
    def normalize(
        deployments: Union[
            MODEL_KEY, List[MODEL_KEY], Dict[MODEL_KEY, "DeploymentConfig"]
        ],
    ) -> Dict[MODEL_KEY, "DeploymentConfig"]:
        """Coerce a model key / list / dict into ``{model_key: DeploymentConfig}``."""
        if isinstance(deployments, dict):
            return deployments
        if isinstance(deployments, str):
            deployments = [deployments]
        return {key: DeploymentConfig() for key in deployments}


class ModelDeployResult(BaseModel):
    """Per-model outcome from a Cluster.deploy call.

    ``replicas`` lists only the replica_ids placed by this call (deploy is
    additive). The contract guarantees exactly one of ``replicas`` / ``error``
    is meaningful, so callers only need to check ``error``.
    """

    replicas: List[REPLICA_ID] = Field(default_factory=list)
    error: Optional[str] = None


class DeployResponse(BaseModel):
    """Aggregate response from Cluster.deploy.

    Attributes:
        results: per-model outcomes keyed by model_key.
        evictions: (model_key, replica_id) pairs evicted to free GPU memory.
        change: whether any cluster state changed — the controller uses this to
            decide whether to run apply().
    """

    results: Dict[MODEL_KEY, ModelDeployResult] = Field(default_factory=dict)
    evictions: Set[Tuple[MODEL_KEY, REPLICA_ID]] = Field(default_factory=set)
    change: bool = False


# ---------- replica state (get_deployment / evict) --------------------------


class ReplicaState(BaseModel):
    """Snapshot of a single Deployment replica.

    Mirrors ``Deployment.get_state()``; built via ``ReplicaState(**state)``.
    """

    model_key: MODEL_KEY
    replica_id: REPLICA_ID
    deployment_level: str  # "hot" / "warm"
    gpus: Dict[int, int]
    size_bytes: int
    pinned: bool
    node_id: Optional[str] = None
    execution_timeout_seconds: Optional[float] = None
    actor_class: Optional[str] = None
    deployed: float


class ReplicaStates(BaseModel):
    """Zero-or-more replica states; return type for get_deployment and evict."""

    replicas: List[ReplicaState] = Field(default_factory=list)
