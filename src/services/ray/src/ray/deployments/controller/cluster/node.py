import logging
import time
import random
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
        gpus_required: Optional[int] = None,
        gpu_ids: Optional[list[int]] = None,
        gpu_mem_bytes_by_id: Optional[Dict[int, int]] = None,
        gpu_memory_required_bytes: Optional[int] = None,
        evictions: Optional[List[MODEL_KEY]] = None,
    ):
        self.candidate_level = candidate_level
        self.gpus_required = gpus_required
        self.gpu_ids = gpu_ids if gpu_ids else []
        self.gpu_mem_bytes_by_id = gpu_mem_bytes_by_id if gpu_mem_bytes_by_id else {}
        self.gpu_memory_required_bytes = gpu_memory_required_bytes
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
    gpu_memory_available_bytes_by_id: Dict[int, int]
    min_available_gpu_fraction: float = 0.3

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

    def _update_available_gpus(self, gpu_id: int) -> None:
        total = self.memory_bytes
        available = self.gpu_memory_available_bytes_by_id[gpu_id]

        if available >= total * self.min_available_gpu_fraction:
            if gpu_id not in self.available:
                self.available.append(gpu_id)
        else:
            if gpu_id in self.available:
                self.available.remove(gpu_id)

    def assign_full_gpus(self, gpus_required: int) -> Dict[int, int]:
        if gpus_required > len(self.available):
            raise ValueError(
                f"Not enough GPUs available to assign {gpus_required} GPUs"
            )

        gpus = self.available[:gpus_required]
        self.available = self.available[gpus_required:]

        gpu_mem_bytes_by_id = {}
        for gpu_id in gpus:
            self.gpu_memory_available_bytes_by_id[gpu_id] = 0
            gpu_mem_bytes_by_id[gpu_id] = self.memory_bytes

        return gpu_mem_bytes_by_id

    def assign_memory(self, required_bytes: int, gpu_id: Optional[int] = None) -> Dict[int, int]:
        if gpu_id is None:
            eligible = [
                (id, available)
                for id, available in self.gpu_memory_available_bytes_by_id.items()
                if available >= required_bytes
            ]
            if not eligible:
                raise ValueError(
                    f"No GPU has enough available memory to assign {required_bytes} bytes"
                )
            gpu_id = max(eligible, key=lambda item: item[1])[0]
        elif self.gpu_memory_available_bytes_by_id.get(gpu_id, 0) < required_bytes:
            raise ValueError(f"GPU {gpu_id} does not have enough available memory")

        self.gpu_memory_available_bytes_by_id[gpu_id] -= required_bytes
        self._update_available_gpus(gpu_id)

        return {gpu_id: required_bytes}

    def assign(self, gpus_required: int) -> list[int]:
        gpu_mem_bytes_by_id = self.assign_full_gpus(gpus_required)
        return list(gpu_mem_bytes_by_id.keys())

    def release(self, indices: list[int]) -> None:
        self.available.extend(indices)

    def release_memory(self, gpu_mem_bytes_by_id: Dict[int, int]) -> None:
        for gpu_id, bytes_used in gpu_mem_bytes_by_id.items():
            self.gpu_memory_available_bytes_by_id[gpu_id] += bytes_used
            self._update_available_gpus(gpu_id)


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
                "available_gpus": self.gpu_resources.available,
                "gpu_memory_available_bytes_by_id": self.gpu_resources.gpu_memory_available_bytes_by_id,
                "cpu_memory_bytes": self.cpu_resources.memory_bytes,
                "available_cpu_memory_bytes": self.cpu_resources.available_memory_bytes,
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
        for eviction in candidate.evictions:
            self.evict(eviction[0], eviction[1], exclude=exclude)

        if (
            candidate.gpu_memory_required_bytes is not None
            and candidate.gpu_memory_required_bytes <= self.gpu_resources.memory_bytes
        ):
            gpu_mem_bytes_by_id = self.gpu_resources.assign_memory(
                candidate.gpu_memory_required_bytes,
                gpu_id=candidate.gpu_ids[0] if candidate.gpu_ids else None,
            )
        else:
            gpu_mem_bytes_by_id = self.gpu_resources.assign_full_gpus(
                candidate.gpus_required
            )

        gpu_memory_fraction = None
        if len(gpu_mem_bytes_by_id.keys()) == 1:
            gpu_id = next(iter(gpu_mem_bytes_by_id.keys()))
            gpu_memory_fraction = min(
                0.99,
                max(
                    0.01,
                    gpu_mem_bytes_by_id[gpu_id] / self.gpu_resources.memory_bytes,
                ),
            )

        self._set_in_map(self.deployments, Deployment(
            model_key=model_key,
            replica_id=replica_id,
            deployment_level=DeploymentLevel.HOT,
            gpu_mem_bytes_by_id=gpu_mem_bytes_by_id,
            gpu_memory_fraction=gpu_memory_fraction,
            size_bytes=size_bytes,
            dedicated=dedicated,
            node_id=self.id,
            execution_timeout_seconds=execution_timeout_seconds,
        ))

        if self._get_from_map(self.cache, model_key, replica_id) is not None:
            self._remove_from_map(self.cache, model_key, replica_id)

            # Return its cpu memory to the node
            self.cpu_resources.release(size_bytes)

    def evict(
        self,
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ):
        deployment = self._get_from_map(self.deployments, model_key, replica_id)
        if deployment is None:
            raise KeyError(f"Deployment not found: {model_key}:{replica_id}")

        self.gpu_resources.release_memory(deployment.gpu_mem_bytes_by_id)

        cpu_memory_needed = (
            deployment.size_bytes - self.cpu_resources.available_memory_bytes
        )

        logger.info(
            f"Evicting {model_key} from {self.name} with cpu memory needed: {cpu_memory_needed} = {deployment.size_bytes} - {self.cpu_resources.available_memory_bytes}"
        )

        if cpu_memory_needed > 0:
            cache_evictions = []

            for eviction_deployment in sorted(
                self._flatten_map(self.cache), key=lambda x: x.size_bytes
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

                    self._remove_from_map(
                        self.cache,
                        eviction_deployment.model_key,
                        eviction_deployment.replica_id,
                    )

                    self.cpu_resources.release(eviction_deployment.size_bytes)

        self._remove_from_map(self.deployments, model_key, replica_id)

        if cpu_memory_needed <= 0:
            self.cpu_resources.allocate(deployment.size_bytes)

            self._set_in_map(self.cache, Deployment(
                model_key=deployment.model_key,
                replica_id=replica_id,
                deployment_level=DeploymentLevel.WARM,
                gpu_mem_bytes_by_id={},
                gpu_memory_fraction=None,
                size_bytes=deployment.size_bytes,
                dedicated=False,
                node_id=self.id,
            ))

    def evictions_for_gpu_count(
        self, gpus_required: int, dedicated: bool = False
    ) -> List[tuple[MODEL_KEY, REPLICA_ID]]:
        deployments = sorted(
            self._evictable_deployments(dedicated=dedicated), key=lambda x: len(x.gpus)
        )

        gpus_needed = gpus_required - len(self.gpu_resources.available)

        evictions = []

        for deployment in deployments:
            evictions.append(self._deployment_key(deployment))

            gpus_needed -= len(deployment.gpus)

            if gpus_needed <= 0:
                return evictions

        return list()

    def evictions_for_fractional_gpu_memory(
        self, required_bytes: int, dedicated: bool = False
    ) -> Optional[tuple[int, List[tuple[MODEL_KEY, REPLICA_ID]]]]:
        best_gpu_id = None
        best_evictions: List[tuple[MODEL_KEY, REPLICA_ID]] = []
        evictable_deployments = self._evictable_deployments(dedicated=dedicated)

        # shuffle the available GPUs to avoid bias
        available_gpu_ids = list(self.gpu_resources.gpu_memory_available_bytes_by_id.keys())
        random.shuffle(available_gpu_ids)
        for gpu_id in available_gpu_ids:
            available_bytes = self.gpu_resources.gpu_memory_available_bytes_by_id[gpu_id]
            if available_bytes >= required_bytes:
                return gpu_id, []

            needed = required_bytes - available_bytes
            candidates = [
                deployment
                for deployment in evictable_deployments
                if gpu_id in deployment.gpu_mem_bytes_by_id
            ]
            candidates = sorted(
                candidates, key=lambda x: x.gpu_mem_bytes_by_id.get(gpu_id, 0)
            )

            evictions: List[tuple[MODEL_KEY, REPLICA_ID]] = []
            for deployment in candidates:
                evictions.append(self._deployment_key(deployment))
                needed -= deployment.gpu_mem_bytes_by_id.get(gpu_id, 0)

                if needed <= 0:
                    if best_gpu_id is None or len(evictions) < len(best_evictions):
                        best_gpu_id = gpu_id
                        best_evictions = evictions
                    break

        if best_gpu_id is None:
            return None

        return best_gpu_id, best_evictions

    def _deployment_key(self, deployment: Deployment) -> tuple[MODEL_KEY, REPLICA_ID]:
        return deployment.model_key, deployment.replica_id

    def _is_evictable(self, deployment: Deployment, dedicated: bool) -> bool:
        if deployment.dedicated:
            return False

        if (
            not dedicated
            and self.minimum_deployment_time_seconds is not None
            and time.time() - deployment.deployed < self.minimum_deployment_time_seconds
        ):
            return False

        return True

    def _evictable_deployments(self, dedicated: bool) -> List[Deployment]:
        return [
            deployment
            for deployment in self._flatten_map(self.deployments)
            if self._is_evictable(deployment, dedicated=dedicated)
        ]

    def evaluate(
        self,
        model_key: MODEL_KEY,
        replica_id: REPLICA_ID,
        model_size_in_bytes: int,
        dedicated: bool = False,
    ) -> Candidate:
        deployment = self._get_from_map(self.deployments, model_key, replica_id)
        if deployment is not None:
            if dedicated:
                deployment.dedicated = True

            return Candidate(candidate_level=CandidateLevel.DEPLOYED)

        cached = self._get_from_map(self.cache, model_key, replica_id) is not None

        # Heuristic for deciding fractional allocation vs full single-GPU allocation.
        gpu_fraction_factor = 3.0
        fraction_largest_possible = 0.8
        fraction_threshold_bytes = int(
            self.gpu_resources.memory_bytes * fraction_largest_possible
        )
        required_bytes = int(model_size_in_bytes * gpu_fraction_factor)
        single_gpu_fit_by_model_size = model_size_in_bytes <= self.gpu_resources.memory_bytes

        # One-GPU eligibility is decided by evaluator size (real model size).
        # If the estimated requirement is small, use fractional allocation.
        # Otherwise, reserve the full GPU for this single-GPU deployment.
        if single_gpu_fit_by_model_size:
            if required_bytes < fraction_threshold_bytes:
                one_gpu_required_bytes = required_bytes
            else:
                one_gpu_required_bytes = int(self.gpu_resources.memory_bytes)

            selection = self.evictions_for_fractional_gpu_memory(
                one_gpu_required_bytes, dedicated=dedicated
            )
            if selection is not None:
                gpu_id, evictions = selection
                if len(evictions) == 0:
                    candidate_level = CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                else:
                    candidate_level = CandidateLevel.CACHED_AND_FULL if cached else CandidateLevel.FULL
                return Candidate(
                    candidate_level=candidate_level,
                    gpus_required=1,
                    gpu_ids=[gpu_id],
                    gpu_mem_bytes_by_id={gpu_id: one_gpu_required_bytes},
                    gpu_memory_required_bytes=one_gpu_required_bytes,
                    evictions=evictions,
                )

        gpus_required = self.gpu_resources.required(model_size_in_bytes)

        if gpus_required <= len(self.gpu_resources.available):
            return Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                ),
                gpus_required=gpus_required,
                gpu_ids=self.gpu_resources.available[:gpus_required],
                gpu_mem_bytes_by_id={
                    gpu_id: self.gpu_resources.memory_bytes
                    for gpu_id in self.gpu_resources.available[:gpus_required]
                },
                gpu_memory_required_bytes=required_bytes,
            )

        elif gpus_required <= self.gpu_resources.total:
            candidate = Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FULL if cached else CandidateLevel.FULL
                ),
                gpus_required=gpus_required,
                gpu_memory_required_bytes=required_bytes,
            )

            candidate.evictions = self.evictions_for_gpu_count(
                gpus_required, dedicated=dedicated
            )

            if len(candidate.evictions) == 0:
                candidate.candidate_level = CandidateLevel.CANT_ACCOMMODATE

            return candidate

        else:
            return Candidate(candidate_level=CandidateLevel.CANT_ACCOMMODATE)

    def purge(self):
        for deployment in self._flatten_map(self.deployments):
            deployment.delete()
        for cache in self._flatten_map(self.cache):
            cache.delete()
