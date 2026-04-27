import logging
import math
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, List, Optional, Set, Union

from ......common.types import MODEL_KEY, NODE_ID
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
        gpus: Optional[Dict[int, int]] = None,
        evictions: Optional[List[MODEL_KEY]] = None,
    ):
        self.candidate_level = candidate_level
        self.gpus = gpus if gpus else {}
        self.evictions = evictions if evictions else []


@dataclass
class GPU:
    index: int
    memory_bytes: int
    available_memory_bytes: int = None

    def __post_init__(self):
        if self.available_memory_bytes is None:
            self.available_memory_bytes = self.memory_bytes

    def fits(self, model_size_bytes: int) -> bool:
        return self.available_memory_bytes >= model_size_bytes


@dataclass
class GPUResources:
    gpu_type: str
    gpus: list[GPU]

    @property
    def total(self) -> int:
        return len(self.gpus)

    @property
    def memory_bytes(self) -> int:
        return self.gpus[0].memory_bytes if self.gpus else 0

    def required(self, model_size_in_bytes: int) -> int:
        if self.memory_bytes == 0:
            raise ValueError("GPU memory bytes is 0")

        return math.ceil(model_size_in_bytes / self.memory_bytes)

    def fitting(self, model_size_bytes: int) -> list[GPU]:
        """GPUs that can fit the given model size, sorted by least available memory first (best-fit)."""
        return sorted(
            [gpu for gpu in self.gpus if gpu.fits(model_size_bytes)],
            key=lambda g: g.available_memory_bytes,
        )

    def allocate(self, allocation: dict[int, int]) -> None:
        """Deduct GPU memory for a pre-computed allocation.

        Args:
            allocation: dict mapping gpu_index -> bytes to allocate.
        """
        gpu_map = {gpu.index: gpu for gpu in self.gpus}
        for idx, bytes_allocated in allocation.items():
            gpu_map[idx].available_memory_bytes -= bytes_allocated

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
        quantization: Optional[str] = None,
        actor_class: Optional[Union[str, type]] = None,
    ):
        # Evict the models from GPU that are needed to deploy the new model
        for eviction in candidate.evictions:
            self.evict(eviction, exclude=exclude)

        self.gpu_resources.allocate(candidate.gpus)

        self.deployments[model_key] = Deployment(
            model_key=model_key,
            deployment_level=DeploymentLevel.HOT,
            gpus=candidate.gpus,
            size_bytes=size_bytes,
            dedicated=dedicated,
            node_id=self.id,
            execution_timeout_seconds=execution_timeout_seconds,
            quantization=quantization,
            actor_class=actor_class,
        )

        if model_key in self.cache:
            del self.cache[model_key]

            # Return its cpu memory to the node
            self.cpu_resources.release(size_bytes)

    def find_cache_evictions(
        self, size_bytes: int, exclude: Optional[Set[MODEL_KEY]] = None
    ) -> Optional[List[Deployment]]:
        """Find the smallest set of cached deployments to evict to free size_bytes of CPU memory.

        Greedily selects cached deployments by size ascending (cheapest first).

        Returns:
            List of cached deployments to evict, or None if not enough can be freed.
        """
        needed = size_bytes - self.cpu_resources.available_memory_bytes
        if needed <= 0:
            return []

        evictions = []
        for deployment in sorted(self.cache.values(), key=lambda x: x.size_bytes):
            if exclude is not None and deployment.model_key in exclude:
                continue

            evictions.append(deployment)
            needed -= deployment.size_bytes

            if needed <= 0:
                return evictions

        return None

    def evict(self, model_key: MODEL_KEY, exclude: Optional[Set[MODEL_KEY]] = None):
        deployment = self.deployments[model_key]

        self.gpu_resources.release(deployment.gpus)

        logger.info(
            f"Evicting {model_key} from {self.name}. "
            f"CPU memory needed: {deployment.size_bytes - self.cpu_resources.available_memory_bytes} "
            f"= {deployment.size_bytes} - {self.cpu_resources.available_memory_bytes}"
        )

        cache_evictions = self.find_cache_evictions(deployment.size_bytes, exclude=exclude)

        if cache_evictions is not None:
            for eviction_deployment in cache_evictions:
                logger.info(
                    f"Evicting {eviction_deployment.model_key} from cache in order to make room for {model_key}"
                )
                del self.cache[eviction_deployment.model_key]
                self.cpu_resources.release(eviction_deployment.size_bytes)

        del self.deployments[model_key]

        if cache_evictions is not None:
            self.cpu_resources.allocate(deployment.size_bytes)

            self.cache[model_key] = Deployment(
                model_key=deployment.model_key,
                deployment_level=DeploymentLevel.WARM,
                gpus={},
                size_bytes=deployment.size_bytes,
                dedicated=False,
                node_id=self.id,
                actor_class=deployment.actor_class,
            )

    def evictable(self, deployment: Deployment, dedicated: bool) -> bool:
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

    def find_evictions(
        self,
        gpus_needed: int,
        per_gpu_bytes: int,
        dedicated: bool = False,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ) -> tuple[List[MODEL_KEY], Dict[int, int]]:
        """Find cheapest evictions to make gpus_needed GPUs each have per_gpu_bytes available.

        Handles both single-GPU and multi-GPU uniformly: a fractional model is
        gpus_needed=1 with per_gpu_bytes=model_size, while a multi-GPU model is
        gpus_needed=N with per_gpu_bytes=full_gpu_memory.

        Algorithm:
            1. Build an occupant map: for each GPU, record which evictable models
               occupy it and how many bytes each uses on that GPU.
            2. Per-GPU eviction plan: for each GPU, compute the minimal set of
               evictions to reach per_gpu_bytes free. Occupants are sorted by size
               ascending (cheapest first) and greedily selected. GPUs that already
               have enough room get an empty plan. GPUs that can't be freed enough
               (unevictable models block it) are skipped.
            3. Pick cheapest GPUs: sort feasible plans by eviction count and take
               the top gpus_needed. E.g. if we need 2 GPUs, prefer the two that
               require the fewest evictions.
            4. Collect results: union the eviction keys across selected GPUs
               (deduplicating models that span multiple GPUs) and build the
               allocation dict.

        Returns:
            (evictions, gpus) tuple where evictions is a list of model keys to evict
            and gpus is the planned allocation (gpu_index -> bytes).
            Returns ([], {}) if the node can't accommodate.
        """
        # Build per-GPU evictable occupant mapping
        occupants_by_gpu: Dict[int, List[tuple]] = {
            gpu.index: [] for gpu in self.gpu_resources.gpus
        }
        for mk, dep in self.deployments.items():
            if exclude is not None and mk in exclude:
                continue
            if not self.evictable(dep, dedicated):
                continue
            for gpu_idx, alloc_bytes in dep.gpus.items():
                occupants_by_gpu[gpu_idx].append((mk, alloc_bytes))

        # For each GPU, compute cheapest eviction plan to reach per_gpu_bytes available
        gpu_plans: List[tuple[int, List[MODEL_KEY]]] = []
        for gpu in self.gpu_resources.gpus:
            needed = per_gpu_bytes - gpu.available_memory_bytes
            if needed <= 0:
                gpu_plans.append((gpu.index, []))
                continue

            # Sort occupants by bytes ascending (cheapest evictions first)
            occupants = sorted(occupants_by_gpu[gpu.index], key=lambda x: x[1])
            freed = 0
            eviction_keys = []
            for mk, alloc_bytes in occupants:
                eviction_keys.append(mk)
                freed += alloc_bytes
                if freed >= needed:
                    break

            if freed >= needed:
                gpu_plans.append((gpu.index, eviction_keys))

        if len(gpu_plans) < gpus_needed:
            return [], {}

        # Sort by eviction count (cheapest first)
        gpu_plans.sort(key=lambda x: len(x[1]))

        # Collect evictions and build allocation from cheapest gpus_needed plans
        all_evictions: set[MODEL_KEY] = set()
        gpus: Dict[int, int] = {}
        for gpu_index, eviction_keys in gpu_plans[:gpus_needed]:
            all_evictions.update(eviction_keys)
            gpus[gpu_index] = per_gpu_bytes

        return list(all_evictions), gpus

    def evaluate(
        self,
        model_key: MODEL_KEY,
        model_size_in_bytes: int,
        dedicated: bool = False,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ) -> Candidate:
        if model_key in self.deployments:
            if dedicated:
                self.deployments[model_key].dedicated = True

            return Candidate(candidate_level=CandidateLevel.DEPLOYED)

        cached = model_key in self.cache

        # Determine per-GPU requirements.
        # Multi-GPU models are treated as consuming 100% of each GPU they span.
        gpus_needed = self.gpu_resources.required(model_size_in_bytes)

        if gpus_needed > self.gpu_resources.total:
            return Candidate(candidate_level=CandidateLevel.CANT_ACCOMMODATE)

        if gpus_needed == 1:
            per_gpu_bytes = model_size_in_bytes
        else:
            per_gpu_bytes = self.gpu_resources.memory_bytes

        # Find GPUs that can fit, sorted best-fit (least available memory first)
        fitting_gpus = self.gpu_resources.fitting(per_gpu_bytes)

        if len(fitting_gpus) >= gpus_needed:
            gpus = {gpu.index: per_gpu_bytes for gpu in fitting_gpus[:gpus_needed]}

            return Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                ),
                gpus=gpus,
            )

        # Need evictions
        evictions, gpus = self.find_evictions(
            gpus_needed, per_gpu_bytes, dedicated=dedicated, exclude=exclude
        )
        if not gpus:
            return Candidate(candidate_level=CandidateLevel.CANT_ACCOMMODATE)

        return Candidate(
            candidate_level=(
                CandidateLevel.CACHED_AND_FULL if cached else CandidateLevel.FULL
            ),
            gpus=gpus,
            evictions=evictions,
        )

    def purge(self):
        for deployment in self.deployments.values():
            deployment.delete()
        for cache in self.cache.values():
            cache.delete()

    def flush_cache(self) -> dict:
        """Flush all WARM models from CPU cache.

        Returns:
            Dict with flushed model keys and memory freed
        """
        flushed = []
        memory_freed = 0

        for model_key, deployment in list(self.cache.items()):
            memory_freed += deployment.size_bytes
            flushed.append(model_key)
            del self.cache[model_key]

        self.cpu_resources.release(memory_freed)

        logger.info(
            f"Flushed {len(flushed)} WARM model(s) from {self.name}, freed {memory_freed} bytes"
        )

        return {
            "flushed": flushed,
            "memory_freed_bytes": memory_freed,
        }
