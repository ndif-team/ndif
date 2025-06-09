from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Set

from ... import MODEL_KEY
from ..cache_actor import CacheActor
from .deployment import Deployment, DeploymentLevel


class CandidateLevel(Enum):

    DEPLOYED = 0
    CACHED_AND_FREE = 1
    FREE = 2
    EVICTABLE = 3
    FULL = 4


class Candidate:

    def __init__(
        self,
        candidate_level: CandidateLevel,
        gpus_required: Optional[int] = None,
        evictions: Optional[List[MODEL_KEY]] = None,
    ):

        self.candidate_level = candidate_level
        self.gpus_required = gpus_required
        self.evictions = evictions if evictions else []


@dataclass
class Resources:

    total_gpus: int

    gpu_type: str
    gpu_memory_bytes: int

    total_memory_bytes: int

    available_gpus: int = 0

    def gpus_required(self, model_size_in_bytes: int) -> int:

        return model_size_in_bytes // self.gpu_memory_bytes + 1

    def __str__(self):
        return (
            f"Resources("
            f"total_gpus={self.total_gpus}, "
            f"gpu_type={self.gpu_type}, "
            f"gpu_memory_bytes={self.gpu_memory_bytes}, "
            f"total_memory_bytes={self.total_memory_bytes}, "
            f"available_gpus={self.available_gpus}, "
            f")"
        )


class Node:

    def __init__(self, id: str, name: str, resources: Resources):

        self.id = id
        self.name = name
        self.resources = resources

        self.deployments: Dict[MODEL_KEY, Deployment] = {}
        self.cached: Set[MODEL_KEY] = set()

        self.cache_actor = CacheActor.options(
            name=f"CacheActor:{self.id}", resources={f"node:{self.name}": 0.01}
        ).remote(self.resources.total_memory_bytes)

    def deploy(
        self,
        model_key: MODEL_KEY,
        candidate: Candidate,
        dedicated: Optional[bool] = None,
    ):

        if candidate.candidate_level == CandidateLevel.DEPLOYED:

            return

        for eviction in candidate.evictions:

            self.evict(eviction)

        self.deployments[model_key] = Deployment(
            model_key=model_key,
            deployment_level=(
                DeploymentLevel.DEDICATED if dedicated else DeploymentLevel.HOT
            ),
            gpus_required=candidate.gpus_required,
        )

        self.resources.available_gpus -= candidate.gpus_required

    def evict(self, model_key: MODEL_KEY):

        self.resources.available_gpus += self.deployments[model_key].gpus_required

        del self.deployments[model_key]

        self.cached.add(model_key)

    def evaluate(
        self, model_key: MODEL_KEY, model_size_in_bytes: int, dedicated: bool = False
    ) -> Candidate:

        if model_key in self.deployments:

            if dedicated:

                self.deployments[model_key].deployment_level = DeploymentLevel.DEDICATED

            return Candidate(candidate_level=CandidateLevel.DEPLOYED)

        cached = model_key in self.cached

        gpus_required = self.resources.gpus_required(model_size_in_bytes)

        if gpus_required <= self.resources.available_gpus:

            return Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                ),
                gpus_required=gpus_required,
            )

        else:

            return Candidate(candidate_level=CandidateLevel.FULL)
