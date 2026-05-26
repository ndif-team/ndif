"""Schemas for Controller / Cluster RPCs (deploy, evict, get_deployment)."""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from ..types import MODEL_KEY, REPLICA_ID


# ---------- deploy ----------------------------------------------------------


class ModelDeployResult(BaseModel):
    """Per-model outcome from a Cluster.deploy call.

    ``config.replicas`` is additive — every Cluster.deploy adds that many new
    replicas regardless of what's already running — so ``replicas`` lists
    only the replica_ids placed by this call.

    Attributes:
        replicas: replica_ids placed during this call.
        error: human-readable error text if size evaluation or placement
            failed. When set, no further replicas of this model were
            attempted on this call.
    """

    replicas: List[REPLICA_ID] = Field(default_factory=list)
    error: Optional[str] = None


class DeployResponse(BaseModel):
    """Aggregate response from Cluster.deploy.

    Attributes:
        results: per-model outcomes keyed by model_key.
        evictions: (model_key, replica_id) pairs evicted during the call to
            free GPU memory for new placements.
        change: whether any cluster state changed (new replicas placed or
            replicas evicted). Used by the controller to decide whether to
            run apply().
    """

    results: Dict[MODEL_KEY, ModelDeployResult] = Field(default_factory=dict)
    evictions: Set[Tuple[MODEL_KEY, REPLICA_ID]] = Field(default_factory=set)
    change: bool = False


# ---------- replica state (get_deployment / evict) --------------------------


class ReplicaState(BaseModel):
    """Snapshot of a single Deployment replica.

    Mirrors ``Deployment.get_state()``'s shape. Constructed via
    ``ReplicaState(**deployment.get_state())``.
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
    """Wrapper for zero-or-more replica states.

    Return type for Controller.get_deployment and Controller.evict. Both
    methods accept ``(model_key, replica_id=None)`` and surface the matching
    replicas as a list — even when the caller scoped the request to a single
    replica (in which case the list has 0 or 1 entries).
    """

    replicas: List[ReplicaState] = Field(default_factory=list)
