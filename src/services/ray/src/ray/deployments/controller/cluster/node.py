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
        model_size_in_bytes: Optional[int] = None,
        evictions: Optional[List[MODEL_KEY]] = None,
    ):
        self.candidate_level = candidate_level
        self.model_size_in_bytes = model_size_in_bytes
        self.evictions = evictions if evictions else []


@dataclass
class GPU:
    index: int
    memory_bytes: int
    available_memory_bytes: int = None

    def __post_init__(self):
        if self.available_memory_bytes is None:
            self.available_memory_bytes = self.memory_bytes


@dataclass
class GPUResources:
    gpu_type: str
    gpus: list[GPU]

    @property
    def available(self) -> list[int]:
        """GPUs with full memory free (backward compat)."""
        return [gpu.index for gpu in self.gpus if gpu.available_memory_bytes == gpu.memory_bytes]

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

    def can_fit(self, model_size_bytes: int) -> bool:
        """Check if the model can fit on any single GPU without eviction."""
        return any(gpu.available_memory_bytes >= model_size_bytes for gpu in self.gpus)

    def assign(self, model_size_bytes: int) -> dict[int, int]:
        """Assign GPU memory for a model.

        Single-GPU (size <= memory_bytes): best-fit bin-pack onto the GPU with
        the least free memory that still fits the model.

        Multi-GPU (size > memory_bytes): use N fully-free GPUs, split evenly.

        Returns:
            dict mapping gpu_index -> bytes_allocated on that GPU.
        """
        if model_size_bytes <= self.memory_bytes:
            # Single-GPU: best-fit bin-pack
            best_gpu = None
            for gpu in self.gpus:
                if gpu.available_memory_bytes >= model_size_bytes:
                    if best_gpu is None or gpu.available_memory_bytes < best_gpu.available_memory_bytes:
                        best_gpu = gpu

            if best_gpu is None:
                raise ValueError(
                    f"No single GPU has {model_size_bytes} bytes available"
                )

            best_gpu.available_memory_bytes -= model_size_bytes
            return {best_gpu.index: model_size_bytes}
        else:
            # Multi-GPU: need fully-free GPUs
            gpus_needed = self.required(model_size_bytes)
            free_gpus = [gpu for gpu in self.gpus if gpu.available_memory_bytes == gpu.memory_bytes]

            if len(free_gpus) < gpus_needed:
                raise ValueError(
                    f"Not enough free GPUs available: need {gpus_needed}, have {len(free_gpus)}"
                )

            selected = free_gpus[:gpus_needed]
            per_gpu = model_size_bytes // gpus_needed
            remainder = model_size_bytes % gpus_needed

            allocation = {}
            for i, gpu in enumerate(selected):
                alloc = per_gpu + (1 if i < remainder else 0)
                gpu.available_memory_bytes -= alloc
                allocation[gpu.index] = alloc

            return allocation

    def release(self, allocation: dict[int, int]) -> None:
        """Release GPU memory from a previous allocation.

        Args:
            allocation: dict mapping gpu_index -> bytes to release.
        """
        gpu_map = {gpu.index: gpu for gpu in self.gpus}
        for idx, bytes_allocated in allocation.items():
            gpu_map[idx].available_memory_bytes += bytes_allocated


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
                "gpu_details": [
                    {
                        "index": gpu.index,
                        "memory_bytes": gpu.memory_bytes,
                        "available_memory_bytes": gpu.available_memory_bytes,
                    }
                    for gpu in self.gpu_resources.gpus
                ],
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
        execution_timeout_seconds: Optional[float] = None,
    ):
        # Evict the models from GPU that are needed to deploy the new model
        for eviction in candidate.evictions:
            self.evict(eviction, exclude=exclude)

        self.deployments[model_key] = Deployment(
            model_key=model_key,
            deployment_level=DeploymentLevel.HOT,
            gpus=self.gpu_resources.assign(candidate.model_size_in_bytes),
            size_bytes=size_bytes,
            dedicated=dedicated,
            node_id=self.id,
            execution_timeout_seconds=execution_timeout_seconds,
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
                gpus={},
                size_bytes=deployment.size_bytes,
                dedicated=False,
                node_id=self.id,
            )

    def _is_evictable(self, deployment: Deployment, dedicated: bool) -> bool:
        """Check if a deployment can be evicted."""
        if deployment.dedicated:
            return False
        if (
            not dedicated
            and self.minimum_deployment_time_seconds is not None
            and time.time() - deployment.deployed < self.minimum_deployment_time_seconds
        ):
            return False
        return True

    def evictions_for_fractional(self, model_size_bytes: int, dedicated: bool = False) -> List[MODEL_KEY]:
        """Find cheapest evictions to free enough memory on a single GPU for a fractional model.

        For each GPU, find the cheapest set of evictions to free enough memory.
        Pick the GPU needing the fewest evictions.
        """
        # Build mapping: gpu_index -> list of (model_key, bytes_on_this_gpu)
        occupants_by_gpu: Dict[int, List[tuple]] = {gpu.index: [] for gpu in self.gpu_resources.gpus}
        for mk, dep in self.deployments.items():
            if not self._is_evictable(dep, dedicated):
                continue
            for gpu_idx, alloc_bytes in dep.gpus.items():
                occupants_by_gpu[gpu_idx].append((mk, alloc_bytes))

        best_evictions = None

        for gpu in self.gpu_resources.gpus:
            needed = model_size_bytes - gpu.available_memory_bytes
            if needed <= 0:
                # Already fits, no evictions needed
                return []

            # Sort occupants by bytes ascending (cheapest evictions first)
            occupants = sorted(occupants_by_gpu[gpu.index], key=lambda x: x[1])
            freed = 0
            candidate_evictions = []
            for mk, alloc_bytes in occupants:
                # Evicting this model frees ALL its GPU memory (across all GPUs),
                # but we only care about this GPU's contribution for the needed check
                candidate_evictions.append(mk)
                freed += alloc_bytes
                if freed >= needed:
                    break

            if freed >= needed:
                if best_evictions is None or len(candidate_evictions) < len(best_evictions):
                    best_evictions = candidate_evictions

        return best_evictions if best_evictions is not None else []

    def evictions_for_whole_gpus(self, gpus_required: int, dedicated: bool = False) -> List[MODEL_KEY]:
        """Find evictions to free enough whole GPUs for a multi-GPU model.

        Find GPUs with fewest occupants, evict all occupants from those GPUs
        until enough are fully free.
        """
        available_count = len(self.gpu_resources.available)
        if available_count >= gpus_required:
            return []

        gpus_still_needed = gpus_required - available_count

        # Build mapping: gpu_index -> set of model_keys occupying it
        occupants_by_gpu: Dict[int, Set[MODEL_KEY]] = {gpu.index: set() for gpu in self.gpu_resources.gpus}
        for mk, dep in self.deployments.items():
            if not self._is_evictable(dep, dedicated):
                continue
            for gpu_idx in dep.gpus.keys():
                occupants_by_gpu[gpu_idx].add(mk)

        # Only consider non-free GPUs that have evictable occupants
        non_free_gpus = [
            (gpu.index, occupants_by_gpu[gpu.index])
            for gpu in self.gpu_resources.gpus
            if gpu.available_memory_bytes < gpu.memory_bytes and len(occupants_by_gpu[gpu.index]) > 0
        ]

        # Sort by fewest occupants first (cheapest to fully free)
        non_free_gpus.sort(key=lambda x: len(x[1]))

        evictions = set()
        freed_gpus = 0

        for gpu_idx, occupant_keys in non_free_gpus:
            evictions.update(occupant_keys)
            freed_gpus += 1
            if freed_gpus >= gpus_still_needed:
                return list(evictions)

        return list()

    def evaluate(
        self, model_key: MODEL_KEY, model_size_in_bytes: int, dedicated: bool = False
    ) -> Candidate:
        if model_key in self.deployments:
            if dedicated:
                self.deployments[model_key].dedicated = True

            return Candidate(candidate_level=CandidateLevel.DEPLOYED)

        cached = model_key in self.cache

        is_single_gpu = model_size_in_bytes <= self.gpu_resources.memory_bytes

        if is_single_gpu:
            # Single-GPU (fractional): check if any GPU has room
            if self.gpu_resources.can_fit(model_size_in_bytes):
                return Candidate(
                    candidate_level=(
                        CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                    ),
                    model_size_in_bytes=model_size_in_bytes,
                )
            else:
                # Need evictions on a single GPU
                candidate = Candidate(
                    candidate_level=(
                        CandidateLevel.CACHED_AND_FULL if cached else CandidateLevel.FULL
                    ),
                    model_size_in_bytes=model_size_in_bytes,
                )
                candidate.evictions = self.evictions_for_fractional(model_size_in_bytes, dedicated=dedicated)
                if len(candidate.evictions) == 0:
                    candidate.candidate_level = CandidateLevel.CANT_ACCOMMODATE
                return candidate
        else:
            # Multi-GPU: need whole dedicated GPUs
            gpus_required = self.gpu_resources.required(model_size_in_bytes)

            if gpus_required > self.gpu_resources.total:
                return Candidate(candidate_level=CandidateLevel.CANT_ACCOMMODATE)

            if gpus_required <= len(self.gpu_resources.available):
                return Candidate(
                    candidate_level=(
                        CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                    ),
                    model_size_in_bytes=model_size_in_bytes,
                )
            else:
                candidate = Candidate(
                    candidate_level=(
                        CandidateLevel.CACHED_AND_FULL if cached else CandidateLevel.FULL
                    ),
                    model_size_in_bytes=model_size_in_bytes,
                )
                candidate.evictions = self.evictions_for_whole_gpus(gpus_required, dedicated=dedicated)
                if len(candidate.evictions) == 0:
                    candidate.candidate_level = CandidateLevel.CANT_ACCOMMODATE
                return candidate

    def purge(self):
        for deployment in self.deployments.values():
            deployment.delete()
        for cache in self.cache.values():
            cache.delete()
