import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Dict, List, Optional, Set

from ... import MODEL_KEY
from .deployment import Deployment, DeploymentLevel


class CandidateLevel(IntEnum):

    DEPLOYED = 0
    CACHED_AND_FREE = 1
    FREE = 2
    CACHED_AND_FULL = 3
    FULL = 4
    CANT_ACCOMMODATE = 5


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

    cpu_memory_bytes: int

    available_cpu_memory_bytes: int
    available_gpus: int

    def gpus_required(self, model_size_in_bytes: int) -> int:

        return model_size_in_bytes // (self.gpu_memory_bytes + 1)

    def __str__(self):
        return (
            f"Resources("
            f"total_gpus={self.total_gpus}, "
            f"gpu_type={self.gpu_type}, "
            f"gpu_memory_bytes={self.gpu_memory_bytes}, "
            f"cpu_memory_bytes={self.cpu_memory_bytes}, "
            f"available_cpu_memory_bytes={self.available_cpu_memory_bytes}, "
            f"available_gpus={self.available_gpus}, "
            f")"
        )


class Node:

    def __init__(
        self,
        id: str,
        name: str,
        resources: Resources,
        minimum_deployment_time_seconds: float = None,
    ):

        self.id = id
        self.name = name
        self.resources = resources
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds

        self.deployments: Dict[MODEL_KEY, Deployment] = {}
        self.cache: Dict[MODEL_KEY, Deployment] = {}


    def deploy(
        self,
        model_key: MODEL_KEY,
        candidate: Candidate,
        size_bytes: int,
        dedicated: Optional[bool] = None,
    ):

        for eviction in candidate.evictions:

            self.evict(eviction)

        self.deployments[model_key] = Deployment(
            model_key=model_key,
            deployment_level=DeploymentLevel.HOT,
            gpus_required=candidate.gpus_required,
            size_bytes=size_bytes,
            dedicated=dedicated,
        )

        self.resources.available_gpus -= candidate.gpus_required

    def evict(self, model_key: MODEL_KEY):
        
        deployment = self.deployments[model_key]

        self.resources.available_gpus += deployment.gpus_required
        
        if self.resources.available_cpu_memory_bytes < deployment.size_bytes:
            
            cpu_memory_needed = deployment.size_bytes - self.resources.available_cpu_memory_bytes
            
            cache_evictions = []
            
            for eviction_deployment in sorted(self.cache.values(), key=lambda x: x.size_bytes):
                
                cpu_memory_needed -= eviction_deployment.size_bytes
                
                cache_evictions.append(eviction_deployment)

                if cpu_memory_needed <= 0:

                    break
                
            for eviction_deployment in cache_evictions:
                
                eviction_deployment.remove_from_cache()
                
                del self.cache[eviction_deployment.model_key]
                
                self.resources.available_cpu_memory_bytes += eviction_deployment.size_bytes
                
        self.resources.available_cpu_memory_bytes -= deployment.size_bytes
        
        deployment.deployment_level = DeploymentLevel.WARM
        
        self.cache[model_key] = deployment
        
        del self.deployments[model_key]

    def evictions(self, gpus_required: int, dedicated: bool = False) -> List[MODEL_KEY]:
        deployments = sorted(
            list(self.deployments.values()), key=lambda x: x.gpus_required
        )

        gpus_needed = gpus_required - self.resources.available_gpus

        evictions = []

        for deployment in deployments:
            

            if deployment.dedicated:

                continue

            if (
                not dedicated
                and self.minimum_deployment_time_seconds is not None
                and time.time() - deployment.deployed
                < self.minimum_deployment_time_seconds
            ):
                continue

            evictions.append(deployment.model_key)

            gpus_needed -= deployment.gpus_required

            if gpus_needed <= 0:

                return evictions
            
        return list()
            
    def evaluate(
        self, model_key: MODEL_KEY, model_size_in_bytes: int, dedicated: bool = False
    ) -> Candidate:

        if model_key in self.deployments:

            if dedicated:

                self.deployments[model_key].dedicated = True

            return Candidate(candidate_level=CandidateLevel.DEPLOYED)

        cached = model_key in self.cache

        gpus_required = self.resources.gpus_required(model_size_in_bytes)

        if gpus_required <= self.resources.available_gpus:

            return Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                ),
                gpus_required=gpus_required,
            )

        elif gpus_required <= self.resources.total_gpus:
            
            candidate = Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FULL if cached else CandidateLevel.FULL
                ),
                gpus_required=gpus_required,
            )
            
            candidate.evictions = self.evictions(gpus_required, dedicated=dedicated)
            
            if len(candidate.evictions) == 0:
                
                candidate.candidate_level = CandidateLevel.CANT_ACCOMMODATE

            return candidate

        else:

            return Candidate(candidate_level=CandidateLevel.CANT_ACCOMMODATE)
