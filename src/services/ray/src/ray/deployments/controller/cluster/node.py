import logging
import time
import torch
from dataclasses import dataclass, asdict
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
class Resources:
    total_gpus: int

    gpu_type: str

    gpu_memory_bytes: int
    cpu_memory_bytes: int

    available_cpu_memory_bytes: int
    available_gpus: list[int]
    gpu_memory_available_bytes_by_id: Dict[int, int]
    min_available_gpu_fraction: float = 0.3

    def gpus_required(self, model_size_in_bytes: int) -> int:
        if self.gpu_memory_bytes == 0:
            raise ValueError("GPU memory bytes is 0")

        return int(model_size_in_bytes // self.gpu_memory_bytes + 1)

    def _update_available_gpus(self, gpu_id: int) -> None:
        total = self.gpu_memory_bytes
        available = self.gpu_memory_available_bytes_by_id[gpu_id]

        try:
            if torch.cuda.is_available():
                real_free, real_total = torch.cuda.mem_get_info(gpu_id)
                available = min(available, int(real_free))
                total = min(total, int(real_total))
        except Exception:
            pass

        self.gpu_memory_available_bytes_by_id[gpu_id] = available
        if available >= total * self.min_available_gpu_fraction:
            if gpu_id not in self.available_gpus:
                self.available_gpus.append(gpu_id)
        else:
            if gpu_id in self.available_gpus:
                self.available_gpus.remove(gpu_id)

    def assign_full_gpus(self, gpus_required: int) -> Dict[int, int]:
        if gpus_required > len(self.available_gpus):
            raise ValueError(
                f"Not enough GPUs available to assign {gpus_required} GPUs"
            )

        gpus = self.available_gpus[:gpus_required]
        self.available_gpus = self.available_gpus[gpus_required:]

        gpu_mem_bytes_by_id = {}
        for gpu_id in gpus:
            self.gpu_memory_available_bytes_by_id[gpu_id] = 0
            gpu_mem_bytes_by_id[gpu_id] = self.gpu_memory_bytes

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

    def __str__(self):
        return (
            f"Resources("
            f"total_gpus={self.total_gpus}, "
            f"gpu_type={self.gpu_type}, "
            f"gpu_memory_bytes={self.gpu_memory_bytes}, "
            f"cpu_memory_bytes={self.cpu_memory_bytes}, "
            f"available_cpu_memory_bytes={self.available_cpu_memory_bytes}, "
            f"available_gpus={self.available_gpus}, "
            f"gpu_memory_available_bytes_by_id={self.gpu_memory_available_bytes_by_id}, "
            f")"
        )


class Node:
    def __init__(
        self,
        id: NODE_ID,
        name: str,
        resources: Resources,
        minimum_deployment_time_seconds: float = None,
        gpu_memory_coefficient: float = 3.0,
        gpu_memory_buffer_bytes: int = 512 * 1024 * 1024,
    ):
        self.id = id
        self.name = name
        self.resources = resources
        self.minimum_deployment_time_seconds = minimum_deployment_time_seconds
        self.gpu_memory_coefficient = gpu_memory_coefficient
        self.gpu_memory_buffer_bytes = gpu_memory_buffer_bytes

        self.deployments: Dict[tuple[MODEL_KEY, int], Deployment] = {}
        self.cache: Dict[tuple[MODEL_KEY, int], Deployment] = {}

    def gpu_memory_required_bytes(self, model_size_in_bytes: int) -> int:
        return int(model_size_in_bytes * self.gpu_memory_coefficient + self.gpu_memory_buffer_bytes)

    def get_state(self) -> Dict[str, Any]:
        """Get the state of the node."""

        return {
            "id": self.id,
            "name": self.name,
            "resources": asdict(self.resources),
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
        replica_id: int,
        candidate: Candidate,
        size_bytes: int,
        dedicated: Optional[bool] = None,
        exclude: Optional[Set[MODEL_KEY]] = None,
    ):
        # Evict the models from GPU that are needed to deploy the new model
        for eviction in candidate.evictions:
            self.evict(eviction[0], eviction[1], exclude=exclude)

        if (
            candidate.gpu_memory_required_bytes is not None
            and candidate.gpu_memory_required_bytes <= self.resources.gpu_memory_bytes
        ):
            gpu_mem_bytes_by_id = self.resources.assign_memory(
                candidate.gpu_memory_required_bytes,
                gpu_id=candidate.gpu_ids[0] if candidate.gpu_ids else None,
            )
        else:
            gpu_mem_bytes_by_id = self.resources.assign_full_gpus(
                candidate.gpus_required
            )

        gpu_memory_fraction = None
        if len(gpu_mem_bytes_by_id.keys()) == 1:
            gpu_id = next(iter(gpu_mem_bytes_by_id.keys()))
            gpu_memory_fraction = min(
                0.99,
                max(
                    0.01,
                    gpu_mem_bytes_by_id[gpu_id] / self.resources.gpu_memory_bytes,
                ),
            )

        self.deployments[(model_key, replica_id)] = Deployment(
            model_key=model_key,
            replica_id=replica_id,
            deployment_level=DeploymentLevel.HOT,
            gpu_mem_bytes_by_id=gpu_mem_bytes_by_id,
            gpu_memory_fraction=gpu_memory_fraction,
            size_bytes=size_bytes,
            dedicated=dedicated,
            node_id=self.id,
        )

        if (model_key, replica_id) in self.cache:
            del self.cache[(model_key, replica_id)]

            # Return its cpu memory to the node
            self.resources.available_cpu_memory_bytes += size_bytes

    def evict(
        self,
        model_key: MODEL_KEY,
        replica_id: int,
        exclude: Optional[Set[MODEL_KEY]] = None,
        cache: bool = True,
    ):
        deployment = self.deployments[(model_key, replica_id)]

        for gpu_id, bytes_used in deployment.gpu_mem_bytes_by_id.items():
            self.resources.gpu_memory_available_bytes_by_id[gpu_id] += bytes_used
            self.resources._update_available_gpus(gpu_id)

        cpu_memory_needed = (
            deployment.size_bytes - self.resources.available_cpu_memory_bytes
        )

        logger.info(
            f"Evicting {model_key} from {self.name} with cpu memory needed: {cpu_memory_needed} = {deployment.size_bytes} - {self.resources.available_cpu_memory_bytes}"
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

                    self.resources.available_cpu_memory_bytes += (
                        eviction_deployment.size_bytes
                    )

        del self.deployments[(model_key, replica_id)]

        if cpu_memory_needed <= 0 and cache:
            self.resources.available_cpu_memory_bytes -= deployment.size_bytes

            self.cache[(model_key, replica_id)] = Deployment(
                model_key=deployment.model_key,
                replica_id=replica_id,
                deployment_level=DeploymentLevel.WARM,
                gpu_mem_bytes_by_id={},
                gpu_memory_fraction=None,
                size_bytes=deployment.size_bytes,
                dedicated=False,
                node_id=self.id,
            )

    def evictions(self, gpus_required: int, dedicated: bool = False) -> List[tuple[MODEL_KEY, int]]:
        deployments = sorted(list(self.deployments.values()), key=lambda x: len(x.gpus))

        gpus_needed = gpus_required - len(self.resources.available_gpus)

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

            evictions.append((deployment.model_key, deployment.replica_id))

            gpus_needed -= len(deployment.gpus)

            if gpus_needed <= 0:
                return evictions

        return list()

    def evictions_for_gpu_memory(
        self, required_bytes: int, dedicated: bool = False
    ) -> Optional[tuple[int, List[tuple[MODEL_KEY, int]]]]:
        best_gpu_id = None
        best_evictions: List[tuple[MODEL_KEY, int]] = []

        for gpu_id, available_bytes in self.resources.gpu_memory_available_bytes_by_id.items():
            if available_bytes >= required_bytes:
                return gpu_id, []

            needed = required_bytes - available_bytes
            candidates = [
                deployment
                for deployment in self.deployments.values()
                if gpu_id in deployment.gpu_mem_bytes_by_id
            ]
            candidates = sorted(
                candidates, key=lambda x: x.gpu_mem_bytes_by_id.get(gpu_id, 0)
            )

            evictions: List[tuple[MODEL_KEY, int]] = []
            for deployment in candidates:
                if deployment.dedicated:
                    continue

                if (
                    not dedicated
                    and self.minimum_deployment_time_seconds is not None
                    and time.time() - deployment.deployed
                    < self.minimum_deployment_time_seconds
                ):
                    continue

                evictions.append((deployment.model_key, deployment.replica_id))
                needed -= deployment.gpu_mem_bytes_by_id.get(gpu_id, 0)

                if needed <= 0:
                    if best_gpu_id is None or len(evictions) < len(best_evictions):
                        best_gpu_id = gpu_id
                        best_evictions = evictions
                    break

        if best_gpu_id is None:
            return None

        return best_gpu_id, best_evictions

    def evaluate(
        self, model_key: MODEL_KEY, replica_id: int, model_size_in_bytes: int, dedicated: bool = False
    ) -> Candidate:
        if (model_key, replica_id) in self.deployments:
            if dedicated:
                self.deployments[(model_key, replica_id)].dedicated = True

            return Candidate(candidate_level=CandidateLevel.DEPLOYED)

        cached = model_key in self.cache

        required_bytes = self.gpu_memory_required_bytes(model_size_in_bytes)

        if required_bytes <= self.resources.gpu_memory_bytes:
            selection = self.evictions_for_gpu_memory(required_bytes, dedicated=dedicated)
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
                    gpu_mem_bytes_by_id={gpu_id: required_bytes},
                    gpu_memory_required_bytes=required_bytes,
                    evictions=evictions,
                )

        gpus_required = self.resources.gpus_required(model_size_in_bytes)

        if gpus_required <= len(self.resources.available_gpus):
            return Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FREE if cached else CandidateLevel.FREE
                ),
                gpus_required=gpus_required,
                gpu_ids=self.resources.available_gpus[:gpus_required],
                gpu_mem_bytes_by_id={
                    gpu_id: self.resources.gpu_memory_bytes
                    for gpu_id in self.resources.available_gpus[:gpus_required]
                },
                gpu_memory_required_bytes=required_bytes,
            )

        elif gpus_required <= self.resources.total_gpus:
            candidate = Candidate(
                candidate_level=(
                    CandidateLevel.CACHED_AND_FULL if cached else CandidateLevel.FULL
                ),
                gpus_required=gpus_required,
                gpu_memory_required_bytes=required_bytes,
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
