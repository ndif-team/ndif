import logging
import math
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, List, Optional, Set

from .....types import MODEL_KEY, NODE_ID, REPLICA_ID
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
        evictions: Optional[List[tuple[MODEL_KEY, REPLICA_ID]]] = None,
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

        self.deployments: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]] = {}
        self.cache: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]] = {}

    def _get_from_map(
        self,
        store: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]],
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
    ) -> Optional[Deployment]:
        return store.get(model_key, {}).get(replica_id)

    def _set_in_map(
        self,
        store: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]],
        deployment: Deployment,
    ) -> None:
        store.setdefault(deployment.model_key, {})[deployment.replica_id] = deployment

    def _remove_from_map(
        self,
        store: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]],
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
    ) -> Optional[Deployment]:
        model_map = store.get(model_key)
        if not model_map:
            return None
        deployment = model_map.pop(replica_id, None)
        if not model_map:
            del store[model_key]
        return deployment

    def _flatten_map(
        self, store: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]]
    ) -> List[Deployment]:
        return [
            deployment
            for model_map in store.values()
            for deployment in model_map.values()
        ]

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the node."""

        deployments_state: List[Dict[str, Any]] = []
        num_deployments = 0
        for model_map in self.deployments.values():
            for deployment in model_map.values():
                deployments_state.append(deployment.get_state())
                num_deployments += 1

        cache_state: List[Dict[str, Any]] = []
        cache_size = 0
        for model_map in self.cache.values():
            for deployment in model_map.values():
                cache_state.append(deployment.get_state())
                cache_size += deployment.size_bytes

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
            "deployments": deployments_state,
            "num_deployments": num_deployments,
            "cache": cache_state,
            "cache_size": cache_size,
        }

    def deploy(
        self,
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
        candidate: Candidate,
        size_bytes: int,
        dedicated: Optional[bool] = None,
        exclude: Optional[Set[MODEL_KEY]] = None,
        execution_timeout_seconds: Optional[float] = None,
    ):
        # Evict the models from GPU that are needed to deploy the new model
        for eviction_model_key, eviction_replica_id in candidate.evictions:
            self.evict(eviction_model_key, eviction_replica_id, exclude=exclude)

        self.gpu_resources.allocate(candidate.gpus)

        self._set_in_map(
            self.deployments,
            Deployment(
                model_key=model_key,
                replica_id=replica_id,
                deployment_level=DeploymentLevel.HOT,
                gpus=candidate.gpus,
                size_bytes=size_bytes,
                dedicated=dedicated,
                node_id=self.id,
                execution_timeout_seconds=execution_timeout_seconds,
            ),
        )

        if self._get_from_map(self.cache, model_key, replica_id) is not None:
            self._remove_from_map(self.cache, model_key, replica_id)

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
        for deployment in sorted(
            self._flatten_map(self.cache), key=lambda x: x.size_bytes
        ):
            if exclude is not None and deployment.model_key in exclude:
                continue

            evictions.append(deployment)
            needed -= deployment.size_bytes

            if needed <= 0:
                return evictions

        return None

    def evict(
        self,
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ):
        deployment = self._get_from_map(self.deployments, model_key, replica_id)
        if deployment is None:
            raise KeyError(f"Deployment not found: {model_key}:{replica_id}")

        self.gpu_resources.release(deployment.gpus)

        logger.info(
            f"Evicting {model_key}:{replica_id} from {self.name}. "
            f"CPU memory needed: {deployment.size_bytes - self.cpu_resources.available_memory_bytes} "
            f"= {deployment.size_bytes} - {self.cpu_resources.available_memory_bytes}"
        )

        cache_evictions = self.find_cache_evictions(
            deployment.size_bytes, exclude=exclude
        )

        if cache_evictions is not None:
            for eviction_deployment in cache_evictions:
                logger.info(
                    f"Evicting {eviction_deployment.model_key}:{eviction_deployment.replica_id} "
                    f"from cache in order to make room for {model_key}"
                )
                self._remove_from_map(
                    self.cache,
                    eviction_deployment.model_key,
                    eviction_deployment.replica_id,
                )
                self.cpu_resources.release(eviction_deployment.size_bytes)

        self._remove_from_map(self.deployments, model_key, replica_id)

        if cache_evictions is not None:
            self.cpu_resources.allocate(deployment.size_bytes)

            self._set_in_map(
                self.cache,
                Deployment(
                    model_key=deployment.model_key,
                    replica_id=replica_id,
                    deployment_level=DeploymentLevel.WARM,
                    gpus={},
                    size_bytes=deployment.size_bytes,
                    dedicated=False,
                    node_id=self.id,
                ),
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

    def find_evictions(
        self,
        gpus_needed: int,
        per_gpu_bytes: int,
        dedicated: bool = False,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ) -> tuple[List[tuple[MODEL_KEY, REPLICA_ID]], Dict[int, int]]:
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
            (evictions, gpus) tuple where evictions is a list of (model_key, replica_id)
            tuples to evict and gpus is the planned allocation (gpu_index -> bytes).
            Returns ([], {}) if the node can't accommodate.
        """
        # Build per-GPU evictable occupant mapping
        occupants_by_gpu: Dict[int, List[tuple]] = {
            gpu.index: [] for gpu in self.gpu_resources.gpus
        }
        for dep in self._flatten_map(self.deployments):
            if exclude is not None and dep.model_key in exclude:
                continue
            if not self._is_evictable(dep, dedicated):
                continue
            for gpu_idx, alloc_bytes in dep.gpus.items():
                occupants_by_gpu[gpu_idx].append(
                    (dep.model_key, dep.replica_id, alloc_bytes)
                )

        # For each GPU, compute cheapest eviction plan to reach per_gpu_bytes available
        gpu_plans: List[tuple[int, List[tuple[MODEL_KEY, REPLICA_ID]]]] = []
        for gpu in self.gpu_resources.gpus:
            needed = per_gpu_bytes - gpu.available_memory_bytes
            if needed <= 0:
                gpu_plans.append((gpu.index, []))
                continue

            # Sort occupants by bytes ascending (cheapest evictions first)
            occupants = sorted(occupants_by_gpu[gpu.index], key=lambda x: x[2])
            freed = 0
            eviction_keys = []
            for mk, rid, alloc_bytes in occupants:
                eviction_keys.append((mk, rid))
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
        all_evictions: set[tuple[MODEL_KEY, REPLICA_ID]] = set()
        gpus: Dict[int, int] = {}
        for gpu_index, eviction_keys in gpu_plans[:gpus_needed]:
            all_evictions.update(eviction_keys)
            gpus[gpu_index] = per_gpu_bytes

        return list(all_evictions), gpus

    def evaluate(
        self,
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
        model_size_in_bytes: int,
        dedicated: bool = False,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ) -> Candidate:
        deployment = self._get_from_map(self.deployments, model_key, replica_id)
        if deployment is not None:
            if dedicated:
                deployment.dedicated = True

            return Candidate(candidate_level=CandidateLevel.DEPLOYED)

        cached = self._get_from_map(self.cache, model_key, replica_id) is not None

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
        for deployment in self._flatten_map(self.deployments):
            deployment.delete()
        for cache in self._flatten_map(self.cache):
            cache.delete()
