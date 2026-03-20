"""Memory bandwidth benchmarks for cross-NUMA penalty measurement.

These benchmarks measure the actual performance penalty when CPU memory
is allocated on a different NUMA node than the GPU performing transfers.
"""

import ctypes
import ctypes.util
import gc
import os
import platform
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest


def _has_numa_hardware() -> bool:
    if platform.system() != "Linux":
        return False
    numa_nodes = Path("/sys/devices/system/node")
    if not numa_nodes.exists():
        return False
    nodes = list(numa_nodes.glob("node[0-9]*"))
    return len(nodes) >= 2


def _has_gpu() -> bool:
    try:
        import torch
        return torch.cuda.is_available() and torch.cuda.device_count() > 0
    except ImportError:
        return False


def _has_libnuma() -> bool:
    lib_path = ctypes.util.find_library("numa")
    if lib_path is None:
        return False
    try:
        lib = ctypes.CDLL(lib_path)
        return lib.numa_available() >= 0
    except (OSError, AttributeError):
        return False


skip_without_numa = pytest.mark.skipif(
    not _has_numa_hardware(),
    reason="Requires multi-node NUMA hardware"
)

skip_without_gpu = pytest.mark.skipif(
    not _has_gpu(),
    reason="Requires NVIDIA GPU"
)

skip_without_libnuma = pytest.mark.skipif(
    not _has_libnuma(),
    reason="Requires libnuma library"
)


def _get_libnuma():
    lib_path = ctypes.util.find_library("numa")
    if lib_path is None:
        return None
    try:
        lib = ctypes.CDLL(lib_path)
        if lib.numa_available() < 0:
            return None
        return lib
    except (OSError, AttributeError):
        return None


def _set_memory_policy_preferred(node_id: int) -> bool:
    lib = _get_libnuma()
    if lib is None:
        return False
    try:
        lib.numa_set_preferred(ctypes.c_int(node_id))
        return True
    except Exception:
        return False


def _get_node_cpus(node_id: int) -> List[int]:
    cpulist_path = Path(f"/sys/devices/system/node/node{node_id}/cpulist")
    if not cpulist_path.exists():
        return []
    content = cpulist_path.read_text().strip()
    cpus = []
    for part in content.split(","):
        if "-" in part:
            start, end = part.split("-", 1)
            cpus.extend(range(int(start), int(end) + 1))
        else:
            cpus.append(int(part))
    return cpus


def _set_cpu_affinity(cpus: List[int]) -> bool:
    try:
        os.sched_setaffinity(0, set(cpus))
        return True
    except (OSError, AttributeError):
        return False


def _get_gpu_numa_node(gpu_index: int) -> Optional[int]:
    """Get the NUMA node for a given GPU using nvidia-smi."""
    import subprocess
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=pci.bus_id", "--format=csv,noheader", f"--id={gpu_index}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        pci_bus_id = result.stdout.strip().lower()
        # nvidia-smi returns "00000000:XX:YY.Z" but sysfs uses "0000:XX:YY.Z"
        # Try both formats
        for pci_id in [pci_bus_id, pci_bus_id.replace("00000000:", "0000:")]:
            numa_path = Path(f"/sys/bus/pci/devices/{pci_id}/numa_node")
            if numa_path.exists():
                node_id = int(numa_path.read_text().strip())
                return node_id if node_id >= 0 else None
    except Exception:
        pass
    return None


def _get_numa_node_count() -> int:
    numa_nodes = Path("/sys/devices/system/node")
    if not numa_nodes.exists():
        return 0
    return len(list(numa_nodes.glob("node[0-9]*")))


def _find_cross_numa_pair() -> Optional[Tuple[int, int, int]]:
    """Find a GPU and two NUMA nodes: one local to GPU, one remote.

    Returns (gpu_index, local_numa_node, remote_numa_node) or None.
    """
    try:
        import torch
        gpu_count = torch.cuda.device_count()
    except ImportError:
        return None

    if gpu_count == 0:
        return None

    numa_count = _get_numa_node_count()
    if numa_count < 2:
        return None

    # Find a GPU with a known NUMA node
    for gpu_idx in range(gpu_count):
        local_node = _get_gpu_numa_node(gpu_idx)
        if local_node is not None:
            # Find a different NUMA node
            for remote_node in range(numa_count):
                if remote_node != local_node:
                    return (gpu_idx, local_node, remote_node)

    return None


class TestCrossNumaCpuToGpu:
    """Benchmark CPU->GPU transfer with local vs remote NUMA memory."""

    @skip_without_gpu
    @skip_without_numa
    @skip_without_libnuma
    @pytest.mark.benchmark
    def test_cpu_to_gpu_transfer_cross_numa(self, benchmark_iterations):
        """Measure CPU->GPU transfer time: local NUMA vs remote NUMA.

        This is the key benchmark: when restoring a model from CPU cache,
        how much slower is it if the CPU memory is on a different NUMA node?
        """
        import torch

        pair = _find_cross_numa_pair()
        if pair is None:
            pytest.skip("Cannot find GPU with cross-NUMA pair")

        gpu_idx, local_node, remote_node = pair

        # Use a realistic model-sized buffer (1GB)
        buffer_size_mb = 1024
        buffer_elements = (buffer_size_mb * 1024 * 1024) // 4  # float32

        local_times = []
        remote_times = []

        for _ in range(benchmark_iterations):
            gc.collect()
            torch.cuda.empty_cache()
            torch.cuda.synchronize()

            # === LOCAL NUMA: allocate CPU tensor on GPU's NUMA node ===
            _set_memory_policy_preferred(local_node)
            cpu_tensor_local = torch.zeros(buffer_elements, dtype=torch.float32)
            cpu_tensor_local.fill_(1.0)  # Touch memory to ensure allocation

            torch.cuda.synchronize()
            start = time.perf_counter()
            gpu_tensor = cpu_tensor_local.to(f"cuda:{gpu_idx}")
            torch.cuda.synchronize()
            local_time = time.perf_counter() - start
            local_times.append(local_time)

            del gpu_tensor, cpu_tensor_local
            gc.collect()
            torch.cuda.empty_cache()

            # === REMOTE NUMA: allocate CPU tensor on different NUMA node ===
            _set_memory_policy_preferred(remote_node)
            cpu_tensor_remote = torch.zeros(buffer_elements, dtype=torch.float32)
            cpu_tensor_remote.fill_(1.0)

            torch.cuda.synchronize()
            start = time.perf_counter()
            gpu_tensor = cpu_tensor_remote.to(f"cuda:{gpu_idx}")
            torch.cuda.synchronize()
            remote_time = time.perf_counter() - start
            remote_times.append(remote_time)

            del gpu_tensor, cpu_tensor_remote
            gc.collect()
            torch.cuda.empty_cache()

        local_avg_ms = (sum(local_times) / len(local_times)) * 1000
        remote_avg_ms = (sum(remote_times) / len(remote_times)) * 1000
        penalty_pct = ((remote_avg_ms - local_avg_ms) / local_avg_ms) * 100
        bandwidth_local = buffer_size_mb / (sum(local_times) / len(local_times)) / 1024  # GB/s
        bandwidth_remote = buffer_size_mb / (sum(remote_times) / len(remote_times)) / 1024

        print(f"\n{'='*60}")
        print(f"CPU -> GPU Transfer (GPU {gpu_idx}, {buffer_size_mb} MB)")
        print(f"{'='*60}")
        print(f"GPU NUMA node:        {local_node}")
        print(f"Remote NUMA node:     {remote_node}")
        print(f"")
        print(f"Local NUMA (aligned):")
        print(f"  Time:      {local_avg_ms:.2f} ms")
        print(f"  Bandwidth: {bandwidth_local:.2f} GB/s")
        print(f"")
        print(f"Remote NUMA (misaligned):")
        print(f"  Time:      {remote_avg_ms:.2f} ms")
        print(f"  Bandwidth: {bandwidth_remote:.2f} GB/s")
        print(f"")
        print(f"Cross-NUMA penalty:   {penalty_pct:.1f}%")
        print(f"{'='*60}")

    @skip_without_gpu
    @skip_without_numa
    @skip_without_libnuma
    @pytest.mark.benchmark
    def test_gpu_to_cpu_transfer_cross_numa(self, benchmark_iterations):
        """Measure GPU->CPU transfer time: local NUMA vs remote NUMA.

        This measures the penalty when offloading a model to CPU cache
        on a remote NUMA node.
        """
        import torch

        pair = _find_cross_numa_pair()
        if pair is None:
            pytest.skip("Cannot find GPU with cross-NUMA pair")

        gpu_idx, local_node, remote_node = pair

        buffer_size_mb = 1024
        buffer_elements = (buffer_size_mb * 1024 * 1024) // 4

        local_times = []
        remote_times = []

        for _ in range(benchmark_iterations):
            gc.collect()
            torch.cuda.empty_cache()

            # Create GPU tensor
            gpu_tensor = torch.zeros(buffer_elements, dtype=torch.float32, device=f"cuda:{gpu_idx}")
            gpu_tensor.fill_(1.0)
            torch.cuda.synchronize()

            # === LOCAL NUMA: move to CPU on GPU's NUMA node ===
            _set_memory_policy_preferred(local_node)

            torch.cuda.synchronize()
            start = time.perf_counter()
            cpu_tensor = gpu_tensor.cpu()
            local_time = time.perf_counter() - start
            local_times.append(local_time)

            del cpu_tensor
            gc.collect()

            # === REMOTE NUMA: move to CPU on different NUMA node ===
            _set_memory_policy_preferred(remote_node)

            torch.cuda.synchronize()
            start = time.perf_counter()
            cpu_tensor = gpu_tensor.cpu()
            remote_time = time.perf_counter() - start
            remote_times.append(remote_time)

            del gpu_tensor, cpu_tensor
            gc.collect()
            torch.cuda.empty_cache()

        local_avg_ms = (sum(local_times) / len(local_times)) * 1000
        remote_avg_ms = (sum(remote_times) / len(remote_times)) * 1000
        penalty_pct = ((remote_avg_ms - local_avg_ms) / local_avg_ms) * 100
        bandwidth_local = buffer_size_mb / (sum(local_times) / len(local_times)) / 1024
        bandwidth_remote = buffer_size_mb / (sum(remote_times) / len(remote_times)) / 1024

        print(f"\n{'='*60}")
        print(f"GPU -> CPU Transfer (GPU {gpu_idx}, {buffer_size_mb} MB)")
        print(f"{'='*60}")
        print(f"GPU NUMA node:        {local_node}")
        print(f"Remote NUMA node:     {remote_node}")
        print(f"")
        print(f"Local NUMA (aligned):")
        print(f"  Time:      {local_avg_ms:.2f} ms")
        print(f"  Bandwidth: {bandwidth_local:.2f} GB/s")
        print(f"")
        print(f"Remote NUMA (misaligned):")
        print(f"  Time:      {remote_avg_ms:.2f} ms")
        print(f"  Bandwidth: {bandwidth_remote:.2f} GB/s")
        print(f"")
        print(f"Cross-NUMA penalty:   {penalty_pct:.1f}%")
        print(f"{'='*60}")

    @skip_without_gpu
    @skip_without_numa
    @skip_without_libnuma
    @pytest.mark.benchmark
    def test_varying_buffer_sizes(self, benchmark_iterations):
        """Measure cross-NUMA penalty across different buffer sizes.

        Tests how the penalty scales with transfer size (small vs large models).
        """
        import torch

        pair = _find_cross_numa_pair()
        if pair is None:
            pytest.skip("Cannot find GPU with cross-NUMA pair")

        gpu_idx, local_node, remote_node = pair

        # Test various model sizes
        buffer_sizes_mb = [64, 256, 512, 1024, 2048]

        print(f"\n{'='*60}")
        print(f"Cross-NUMA Penalty vs Buffer Size (GPU {gpu_idx})")
        print(f"{'='*60}")
        print(f"GPU NUMA: {local_node}, Remote NUMA: {remote_node}")
        print(f"")
        print(f"{'Size (MB)':<12} {'Local (ms)':<12} {'Remote (ms)':<12} {'Penalty':<10}")
        print(f"{'-'*46}")

        for buffer_size_mb in buffer_sizes_mb:
            buffer_elements = (buffer_size_mb * 1024 * 1024) // 4

            local_times = []
            remote_times = []

            for _ in range(benchmark_iterations):
                gc.collect()
                torch.cuda.empty_cache()

                # Local NUMA
                _set_memory_policy_preferred(local_node)
                cpu_tensor = torch.zeros(buffer_elements, dtype=torch.float32)
                cpu_tensor.fill_(1.0)

                torch.cuda.synchronize()
                start = time.perf_counter()
                gpu_tensor = cpu_tensor.to(f"cuda:{gpu_idx}")
                torch.cuda.synchronize()
                local_times.append(time.perf_counter() - start)

                del gpu_tensor, cpu_tensor
                gc.collect()
                torch.cuda.empty_cache()

                # Remote NUMA
                _set_memory_policy_preferred(remote_node)
                cpu_tensor = torch.zeros(buffer_elements, dtype=torch.float32)
                cpu_tensor.fill_(1.0)

                torch.cuda.synchronize()
                start = time.perf_counter()
                gpu_tensor = cpu_tensor.to(f"cuda:{gpu_idx}")
                torch.cuda.synchronize()
                remote_times.append(time.perf_counter() - start)

                del gpu_tensor, cpu_tensor
                gc.collect()
                torch.cuda.empty_cache()

            local_avg_ms = (sum(local_times) / len(local_times)) * 1000
            remote_avg_ms = (sum(remote_times) / len(remote_times)) * 1000
            penalty_pct = ((remote_avg_ms - local_avg_ms) / local_avg_ms) * 100

            print(f"{buffer_size_mb:<12} {local_avg_ms:<12.2f} {remote_avg_ms:<12.2f} {penalty_pct:+.1f}%")

        print(f"{'='*60}")
