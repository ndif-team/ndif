import logging
import math
import time
import uuid
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from ......common.types import MODEL_KEY, NODE_ID, REPLICA_ID
from .deployment import Deployment, DeploymentLevel


def _new_replica_id() -> REPLICA_ID:
    return uuid.uuid4().hex[:5]

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
        evictions: Optional[List[Tuple[MODEL_KEY, REPLICA_ID]]] = None,
    ):
        self.candidate_level = candidate_level
        self.gpus = gpus if gpus else {}
        self.evictions: List[Tuple[MODEL_KEY, REPLICA_ID]] = (
            evictions if evictions else []
        )


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
        ip: Optional[str] = None,
    ):
        self.id = id
        self.name = name
        # Ray node IP — matches the `ip` label on Ray's Prometheus node/GPU
        # metrics, so deployment metrics can be joined against those profiles.
        self.ip = ip
        self.gpu_resources = gpu_resources
        self.cpu_resources = cpu_resources
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds

        self.deployments: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]] = {}
        self.cache: Dict[MODEL_KEY, Dict[REPLICA_ID, Deployment]] = {}

    def iter_deployments(self):
        """Yield (model_key, replica_id, Deployment) for every HOT replica on this node."""
        for model_key, replicas in self.deployments.items():
            for replica_id, deployment in replicas.items():
                yield model_key, replica_id, deployment

    def iter_cache(self):
        """Yield (model_key, replica_id, Deployment) for every WARM replica on this node."""
        for model_key, replicas in self.cache.items():
            for replica_id, deployment in replicas.items():
                yield model_key, replica_id, deployment

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the node."""

        return {
            "id": self.id,
            "name": self.name,
            "ip": self.ip,
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
                deployment.get_state()
                for _, _, deployment in self.iter_deployments()
            ],
            "num_deployments": sum(
                len(replicas) for replicas in self.deployments.values()
            ),
            "cache": [
                deployment.get_state() for _, _, deployment in self.iter_cache()
            ],
            "cache_size": sum(
                deployment.size_bytes for _, _, deployment in self.iter_cache()
            ),
        }

    def deploy(
        self,
        model_key: MODEL_KEY,
        candidate: Candidate,
        size_bytes: int,
        pinned: Optional[bool] = None,
        exclude: Optional[Set[MODEL_KEY]] = None,
        execution_timeout_seconds: Optional[float] = None,
        actor_class: Optional[Union[str, type]] = None,
    ) -> REPLICA_ID:
        """Place a new HOT replica on this node, optionally promoting a WARM replica.

        Returns the replica_id of the newly deployed replica (preserved from the
        WARM replica when promoting from cache, freshly generated otherwise).
        """
        # Evict the models from GPU that are needed to deploy the new model
        for eviction_model_key, eviction_replica_id in candidate.evictions:
            self.evict(eviction_model_key, eviction_replica_id, exclude=exclude)

        self.gpu_resources.allocate(candidate.gpus)

        # WARM->HOT promotion reuses the cached replica_id so the actor name
        # stays stable across the transition. Pick any WARM replica of this
        # model_key (the cluster only tells us "a cache exists", not which one).
        replica_id: Optional[REPLICA_ID] = None
        if model_key in self.cache and self.cache[model_key]:
            replica_id, cached_deployment = next(iter(self.cache[model_key].items()))
            del self.cache[model_key][replica_id]
            if not self.cache[model_key]:
                del self.cache[model_key]
            self.cpu_resources.release(cached_deployment.size_bytes)

        if replica_id is None:
            replica_id = _new_replica_id()

        self.deployments.setdefault(model_key, {})[replica_id] = Deployment(
            model_key=model_key,
            replica_id=replica_id,
            deployment_level=DeploymentLevel.HOT,
            gpus=candidate.gpus,
            size_bytes=size_bytes,
            pinned=pinned,
            node_id=self.id,
            execution_timeout_seconds=execution_timeout_seconds,
            actor_class=actor_class,
        )

        return replica_id

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
        cached_replicas = [dep for _, _, dep in self.iter_cache()]
        for deployment in sorted(cached_replicas, key=lambda x: x.size_bytes):
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
        """Evict a specific replica of a model from this node.

        WARM replicas are dropped immediately. HOT replicas release their GPU
        memory and, if the node has (or can make) CPU room, are demoted to
        WARM (preserving replica_id so the actor name is stable across the
        transition); otherwise they are removed outright.
        """

        if model_key in self.cache and replica_id in self.cache[model_key]:
            cached = self.cache[model_key].pop(replica_id)
            if not self.cache[model_key]:
                del self.cache[model_key]
            self.cpu_resources.release(cached.size_bytes)
            logger.info(
                f"Evicting WARM {model_key}[{replica_id}] from {self.name}, "
                f"freed {cached.size_bytes} bytes"
            )
            return

        deployment = self.deployments[model_key][replica_id]

        self.gpu_resources.release(deployment.gpus)

        logger.info(
            f"Evicting {model_key}[{replica_id}] from {self.name}. "
            f"CPU memory needed: {deployment.size_bytes - self.cpu_resources.available_memory_bytes} "
            f"= {deployment.size_bytes} - {self.cpu_resources.available_memory_bytes}"
        )

        cache_evictions = self.find_cache_evictions(
            deployment.size_bytes, exclude=exclude
        )

        if cache_evictions is not None:
            for eviction_deployment in cache_evictions:
                logger.info(
                    f"Evicting {eviction_deployment.model_key}[{eviction_deployment.replica_id}] "
                    f"from cache in order to make room for {model_key}[{replica_id}]"
                )
                del self.cache[eviction_deployment.model_key][
                    eviction_deployment.replica_id
                ]
                if not self.cache[eviction_deployment.model_key]:
                    del self.cache[eviction_deployment.model_key]
                self.cpu_resources.release(eviction_deployment.size_bytes)

        del self.deployments[model_key][replica_id]
        if not self.deployments[model_key]:
            del self.deployments[model_key]

        if cache_evictions is not None:
            self.cpu_resources.allocate(deployment.size_bytes)

            self.cache.setdefault(model_key, {})[replica_id] = Deployment(
                model_key=deployment.model_key,
                replica_id=replica_id,
                deployment_level=DeploymentLevel.WARM,
                gpus={},
                size_bytes=deployment.size_bytes,
                pinned=False,
                node_id=self.id,
                actor_class=deployment.actor_class,
            )

    def evictable(self, deployment: Deployment, pinned: bool) -> bool:
        """Check if a deployment can be evicted."""
        if deployment.pinned:
            return False
        if (
            not pinned
            and self.minimum_deployment_time_seconds is not None
            and time.time() - deployment.deployed < self.minimum_deployment_time_seconds
        ):
            return False
        return True

    def find_evictions(
        self,
        gpus_needed: int,
        per_gpu_bytes: int,
        pinned: bool = False,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ) -> tuple[List[Tuple[MODEL_KEY, REPLICA_ID]], Dict[int, int]]:
        """Find cheapest evictions to make gpus_needed GPUs each have per_gpu_bytes available.

        Handles both single-GPU and multi-GPU uniformly: a fractional model is
        gpus_needed=1 with per_gpu_bytes=model_size, while a multi-GPU model is
        gpus_needed=N with per_gpu_bytes=full_gpu_memory.

        With replicas, each (model_key, replica_id) is an independent evictable
        unit — two replicas of the same model on the same GPU contribute two
        separate occupant entries.

        Algorithm:
            1. Build an occupant map: for each GPU, record which evictable
               (model_key, replica_id) pairs occupy it and how many bytes each
               uses on that GPU.
            2. Per-GPU eviction plan: for each GPU, compute the minimal set of
               evictions to reach per_gpu_bytes free. Occupants are sorted by size
               ascending (cheapest first) and greedily selected. GPUs that already
               have enough room get an empty plan. GPUs that can't be freed enough
               (unevictable replicas block it) are skipped.
            3. Pick cheapest GPUs: sort feasible plans by eviction count and take
               the top gpus_needed. E.g. if we need 2 GPUs, prefer the two that
               require the fewest evictions.
            4. Collect results: union the eviction pairs across selected GPUs
               (deduplicating replicas that span multiple GPUs) and build the
               allocation dict.

        Returns:
            (evictions, gpus) tuple where evictions is a list of (model_key,
            replica_id) pairs to evict and gpus is the planned allocation
            (gpu_index -> bytes). Returns ([], {}) if the node can't accommodate.
        """
        # Build per-GPU evictable occupant mapping. Each (model_key, replica_id)
        # is its own occupant.
        occupants_by_gpu: Dict[int, List[tuple]] = {
            gpu.index: [] for gpu in self.gpu_resources.gpus
        }
        for mk, replicas in self.deployments.items():
            if exclude is not None and mk in exclude:
                continue
            for rid, dep in replicas.items():
                if not self.evictable(dep, pinned):
                    continue
                for gpu_idx, alloc_bytes in dep.gpus.items():
                    occupants_by_gpu[gpu_idx].append((mk, rid, alloc_bytes))

        # For each GPU, compute cheapest eviction plan to reach per_gpu_bytes available
        gpu_plans: List[Tuple[int, List[Tuple[MODEL_KEY, REPLICA_ID]]]] = []
        for gpu in self.gpu_resources.gpus:
            needed = per_gpu_bytes - gpu.available_memory_bytes
            if needed <= 0:
                gpu_plans.append((gpu.index, []))
                continue

            # Sort occupants by bytes ascending (cheapest evictions first)
            occupants = sorted(occupants_by_gpu[gpu.index], key=lambda x: x[2])
            freed = 0
            eviction_keys: List[Tuple[MODEL_KEY, REPLICA_ID]] = []
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
        all_evictions: set[Tuple[MODEL_KEY, REPLICA_ID]] = set()
        gpus: Dict[int, int] = {}
        for gpu_index, eviction_keys in gpu_plans[:gpus_needed]:
            all_evictions.update(eviction_keys)
            gpus[gpu_index] = per_gpu_bytes

        return list(all_evictions), gpus

    def evaluate(
        self,
        model_key: MODEL_KEY,
        model_size_in_bytes: int,
        pinned: bool = False,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ) -> Candidate:
        cached = model_key in self.cache and bool(self.cache[model_key])

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
            gpus_needed, per_gpu_bytes, pinned=pinned, exclude=exclude
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
        for _, _, deployment in self.iter_deployments():
            deployment.delete()
        for _, _, cached in self.iter_cache():
            cached.delete()
