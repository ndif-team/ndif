#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sys
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DEFAULT_RAY_ADDRESS = "ray://localhost:10001"

MODEL_CANDIDATES = [
    "Qwen/Qwen2.5-1.5B-Instruct",
    "openai-community/gpt2",
    "EleutherAI/pythia-1b",
]


@dataclass
class GpuInfo:
    name: str
    total_memory_bytes: int


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def add_repo_to_path() -> None:
    root = repo_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))


def get_model_key(repo_id: str, revision: str) -> str:
    add_repo_to_path()
    from cli.lib.util import get_model_key as _get_model_key

    return _get_model_key(repo_id, revision)


def get_controller():
    add_repo_to_path()
    from cli.lib.util import get_controller_actor_handle

    return get_controller_actor_handle()


def connect_ray(ray_address: str = DEFAULT_RAY_ADDRESS) -> None:
    import ray

    ray.init(address=ray_address, ignore_reinit_error=True, logging_level="error")


def get_cpu_count() -> int:
    return os.cpu_count() or 0


def _get_gpus_from_torch() -> list[GpuInfo]:
    try:
        import torch

        if not torch.cuda.is_available():
            return []
        gpus = []
        for idx in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(idx)
            gpus.append(GpuInfo(name=props.name, total_memory_bytes=int(props.total_memory)))
        return gpus
    except Exception:
        return []


def _get_gpus_from_nvidia_smi() -> list[GpuInfo]:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
        )
    except Exception:
        return []

    gpus = []
    for line in out.strip().splitlines():
        if not line.strip():
            continue
        name, mem_mb = [part.strip() for part in line.split(",", 1)]
        try:
            mem_bytes = int(float(mem_mb) * 1024 * 1024)
        except ValueError:
            continue
        gpus.append(GpuInfo(name=name, total_memory_bytes=mem_bytes))
    return gpus


def get_gpu_info() -> list[GpuInfo]:
    gpus = _get_gpus_from_torch()
    if gpus:
        return gpus
    return _get_gpus_from_nvidia_smi()


def _hf_api_get(url: str) -> dict:
    try:
        import requests
    except Exception as exc:
        raise RuntimeError("requests is required to query Hugging Face API") from exc

    resp = requests.get(url, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"HF API request failed: {resp.status_code} {resp.text}")
    return resp.json()


def get_model_size_bytes(repo_id: str) -> int:
    data = _hf_api_get(f"https://huggingface.co/api/models/{repo_id}")
    siblings = data.get("siblings", [])

    total = 0
    for item in siblings:
        filename = item.get("rfilename", "")
        size = item.get("size")
        if size is None:
            continue
        lower = filename.lower()
        if lower.endswith((".safetensors", ".bin", ".pt", ".pth", ".gguf")):
            total += int(size)

    if total <= 0:
        raise RuntimeError(f"Could not determine model size for {repo_id}")

    return total


def reserve_bytes_for_model(model_size_bytes: int) -> int:
    return model_size_bytes * 4


def bytes_to_gib(value: int) -> float:
    return value / 1024 / 1024 / 1024


def print_system_overview() -> None:
    cpu_count = get_cpu_count()
    gpus = get_gpu_info()

    print("System resources")
    print(f"  CPUs: {cpu_count}")
    if not gpus:
        print("  GPUs: none detected")
    else:
        print(f"  GPUs: {len(gpus)}")
        for idx, gpu in enumerate(gpus):
            print(
                f"    - GPU {idx}: {gpu.name}, {bytes_to_gib(gpu.total_memory_bytes):.2f} GiB"
            )


def print_model_sizes(models: Iterable[str]) -> dict[str, int]:
    sizes = {}
    print("Model size profiling (HF metadata)")
    for model in models:
        size_bytes = get_model_size_bytes(model)
        reserve_bytes = reserve_bytes_for_model(size_bytes)
        sizes[model] = size_bytes
        print(
            f"  - {model}: {bytes_to_gib(size_bytes):.2f} GiB"
            f" (reserve {bytes_to_gib(reserve_bytes):.2f} GiB)"
        )
    return sizes


def decide_replica_count(model_size_bytes: int) -> int:
    gpus = get_gpu_info()
    if not gpus:
        print("Warning: no GPUs detected; defaulting replica count to 1")
        return 1

    total_gpu_mem = sum(g.total_memory_bytes for g in gpus)
    total_gpus = len(gpus)

    reserve = reserve_bytes_for_model(model_size_bytes)
    if reserve <= 0:
        return 1

    max_by_mem = math.floor(total_gpu_mem / reserve)
    max_by_gpus = total_gpus

    replicas = max(1, min(max_by_mem, max_by_gpus))
    return replicas


def count_hot_replicas(status: dict, model_key: str) -> list[int]:
    deployments = status.get("deployments", {})
    replica_ids = []
    for dep in deployments.values():
        if dep.get("deployment_level") != "HOT":
            continue
        if dep.get("model_key") != model_key:
            continue
        replica_ids.append(int(dep.get("replica_id")))
    return sorted(replica_ids)


def print_status_summary(status: dict) -> None:
    deployments = status.get("deployments", {})
    hot = [d for d in deployments.values() if d.get("deployment_level") == "HOT"]
    warm = [d for d in deployments.values() if d.get("deployment_level") == "WARM"]
    cold = [d for d in deployments.values() if d.get("deployment_level") == "COLD"]
    print(f"Status summary: HOT={len(hot)} WARM={len(warm)} COLD={len(cold)}")


def dump_status(status: dict) -> None:
    print(json.dumps(status, indent=2, default=str))
