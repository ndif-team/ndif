import logging
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, List, Optional, Set

from .....types import MODEL_KEY, NODE_ID
from .deployment import Deployment, DeploymentLevel

logger = logging.getLogger("ndif")


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
class GPU:
    index: int
    memory_bytes: int


@dataclass
class GPUResources:
    gpu_type: str
    gpus: list[GPU]
    available: list[int]

    @property
    def total(self) -> int:
        return len(self.gpus)

    @property
    def memory_bytes(self) -> int:
        return self.gpus[0].memory_bytes if self.gpus else 0

    def required(self, model_size_in_bytes: int) -> int:
        if self.memory_bytes == 0:
            raise ValueError("GPU memory bytes is 0")

        return int(model_size_in_bytes // self.memory_bytes + 1)

    def assign(self, count: int) -> list[int]:
        if count > len(self.available):
            raise ValueError(
                f"Not enough GPUs available to assign {count} GPUs"
            )

        gpus = self.available[:count]
        self.available = self.available[count:]
        return gpus

    def release(self, indices: list[int]) -> None:
        self.available.extend(indices)


@dataclass
class CPUResources:
    memory_bytes: int
    available_memory_bytes: int

    def allocate(self, size_bytes: int) -> None:
        self.available_memory_bytes -= size_bytes

    def release(self, size_bytes: int) -> None:
        self.available_memory_bytes += size_bytes


class Node:
    def __init__(
        self,
        id: NODE_ID,
        name: str,
        gpu_resources: GPUResources,
        cpu_resources: CPUResources,
        minimum_deployment_time_seconds: float = None,
    ):
        self.id = id
        self.name = name
        self.gpu_resources = gpu_resources
        self.cpu_resources = cpu_resources
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds

        self.deployments: Dict[MODEL_KEY, Deployment] = {}
        self.cache: Dict[MODEL_KEY, Deployment] = {}

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the node."""

        return {
            "id": self.id,
            "name": self.name,
            "resources": {
                "gpu_type": self.gpu_resources.gpu_type,
                "total_gpus": self.gpu_resources.total,
                "gpu_memory_bytes": self.gpu_resources.memory_bytes,
                "available_gpus": self.gpu_resources.available,
                "cpu_memory_bytes": self.cpu_resources.memory_bytes,
                "available_cpu_memory_bytes": self.cpu_resources.available_memory_bytes,
            },
            "deployments": [
                deployment.get_state() for deployment in self.deployments.values()
            ],
            "num_deployments": len(self.deployments),
            "cache": [deployment.get_state() for deployment in self.cache.values()],
            "cache_size": sum(
                [deployment.size_bytes for deployment in self.cache.values()]
            ),
        }

    def deploy(
        self,
        model_key: MODEL_KEY,
        candidate: Candidate,
        size_bytes: int,
        dedicated: Optional[bool] = None,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ):
        # Evict the models from GPU that are needed to deploy the new model
        for eviction in candidate.evictions:
            self.evict(eviction, exclude=exclude)

        self.deployments[model_key] = Deployment(
            model_key=model_key,
            deployment_level=DeploymentLevel.HOT,
            gpus=self.gpu_resources.assign(candidate.gpus_required),
            size_bytes=size_bytes,
            dedicated=dedicated,
        )

        if model_key in self.cache:
            del self.cache[model_key]

            # Return its cpu memory to the node
            self.cpu_resources.release(size_bytes)

    def evict(self, model_key: MODEL_KEY, exclude: Optional[Set[MODEL_KEY]] = None):
        deployment = self.deployments[model_key]

        self.gpu_resources.release(deployment.gpus)

        cpu_memory_needed = (
            deployment.size_bytes - self.cpu_resources.available_memory_bytes
        )

        logger.info(
            f"Evicting {model_key} from {self.name} with cpu memory needed: {cpu_memory_needed} = {deployment.size_bytes} - {self.cpu_resources.available_memory_bytes}"
        )

        if cpu_memory_needed > 0:
            cache_evictions = []

            for eviction_deployment in sorted(
                self.cache.values(), key=lambda x: x.size_bytes
            ):
                if exclude is not None and eviction_deployment.model_key in exclude:
                    continue

                cpu_memory_needed -= eviction_deployment.size_bytes

                cache_evictions.append(eviction_deployment)

                if cpu_memory_needed <= 0:
                    break

            if cpu_memory_needed <= 0:
                for eviction_deployment in cache_evictions:
                    logger.info(
                        f"Evicting {eviction_deployment.model_key} from cache in order to make room for {model_key}"
                    )

                    del self.cache[eviction_deployment.model_key]

                    self.cpu_resources.release(eviction_deployment.size_bytes)

        del self.deployments[model_key]

        if cpu_memory_needed <= 0:
            self.cpu_resources.allocate(deployment.size_bytes)

            self.cache[model_key] = Deployment(
                model_key=deployment.model_key,
                deployment_level=DeploymentLevel.WARM,
                gpus=[],
                size_bytes=deployment.size_bytes,
                dedicated=False,
            )

    def evictions(self, gpus_required: int, dedicated: bool = False) -> List[MODEL_KEY]:
        deployments = sorted(list(self.deployments.values()), key=lambda x: len(x.gpus))

        gpus_needed = gpus_required - len(self.gpu_resources.available)

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

            gpus_needed -= len(deployment.gpus)

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

        gpus_required = self.gpu_resources.required(model_size_in_bytes)

        if gpus_required <= len(self.gpu_resources.available):
            return Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                ),
                gpus_required=gpus_required,
            )

        elif gpus_required <= self.gpu_resources.total:
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

    def purge(self):
        for deployment in self.deployments.values():
            deployment.delete()
        for cache in self.cache.values():
            cache.delete()
