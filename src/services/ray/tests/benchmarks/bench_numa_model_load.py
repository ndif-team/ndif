"""Model loading benchmarks for cross-NUMA penalty measurement.

These benchmarks measure the actual performance penalty when loading
and caching models where CPU memory is on a different NUMA node than the GPU.
"""

import ctypes
import ctypes.util
import gc
import os
import platform
import sys
import time
from pathlib import Path
from typing import Optional, Set, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


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


def _set_cpu_affinity_to_node(node_id: int) -> bool:
    cpulist_path = Path(f"/sys/devices/system/node/node{node_id}/cpulist")
    if not cpulist_path.exists():
        return False
    content = cpulist_path.read_text().strip()
    cpus = set()
    for part in content.split(","):
        if "-" in part:
            start, end = part.split("-", 1)
            cpus.update(range(int(start), int(end) + 1))
        else:
            cpus.add(int(part))
    try:
        os.sched_setaffinity(0, cpus)
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

    for gpu_idx in range(gpu_count):
        local_node = _get_gpu_numa_node(gpu_idx)
        if local_node is not None:
            for remote_node in range(numa_count):
                if remote_node != local_node:
                    return (gpu_idx, local_node, remote_node)

    return None


def _make_model_key(repo_id: str) -> str:
    import json
    config = {"repo_id": repo_id, "revision": "main"}
    return f"nnsight.modeling.language.LanguageModel:{json.dumps(config)}"


def _get_model_size_mb(model) -> float:
    """Get model size in MB."""
    total_bytes = sum(p.numel() * p.element_size() for p in model.parameters())
    return total_bytes / (1024 * 1024)


class TestModelCacheRestoreCrossNuma:
    """Benchmark model cache restore (from_cache) with cross-NUMA penalty."""

    @skip_without_gpu
    @skip_without_numa
    @skip_without_libnuma
    @pytest.mark.benchmark
    def test_from_cache_cross_numa(self, benchmark_model, benchmark_iterations):
        """Measure from_cache time: local NUMA vs remote NUMA.

        This is the primary benchmark: restoring a model from CPU cache
        when the cache is on the GPU's NUMA node vs a different NUMA node.
        """
        import torch
        from accelerate import dispatch_model
        from transformers import AutoModelForCausalLM

        pair = _find_cross_numa_pair()
        if pair is None:
            pytest.skip("Cannot find GPU with cross-NUMA pair")

        gpu_idx, local_node, remote_node = pair

        # Set up single GPU
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_idx)

        local_restore_times = []
        remote_restore_times = []

        for iteration in range(benchmark_iterations):
            gc.collect()
            torch.cuda.empty_cache()

            # ============================================================
            # LOCAL NUMA: Load model, cache on local NUMA, restore
            # ============================================================
            _set_memory_policy_preferred(local_node)
            _set_cpu_affinity_to_node(local_node)

            # Load model to GPU
            model = AutoModelForCausalLM.from_pretrained(
                benchmark_model,
                torch_dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            model.requires_grad_(False)
            torch.cuda.synchronize()

            if iteration == 0:
                model_size_mb = _get_model_size_mb(model)

            # Move to CPU cache (on local NUMA node)
            model = model.cpu()
            gc.collect()
            torch.cuda.empty_cache()

            # Measure restore time
            torch.cuda.synchronize()
            start = time.perf_counter()
            model = model.to("cuda:0")
            torch.cuda.synchronize()
            local_restore_time = time.perf_counter() - start
            local_restore_times.append(local_restore_time)

            del model
            gc.collect()
            torch.cuda.empty_cache()

            # ============================================================
            # REMOTE NUMA: Load model, cache on remote NUMA, restore
            # ============================================================
            _set_memory_policy_preferred(remote_node)
            _set_cpu_affinity_to_node(remote_node)

            # Load model to GPU
            model = AutoModelForCausalLM.from_pretrained(
                benchmark_model,
                torch_dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            model.requires_grad_(False)
            torch.cuda.synchronize()

            # Move to CPU cache (on remote NUMA node)
            model = model.cpu()
            gc.collect()
            torch.cuda.empty_cache()

            # Restore back to GPU - but memory is on wrong NUMA node
            # Reset affinity to local node (simulating normal operation)
            _set_cpu_affinity_to_node(local_node)

            torch.cuda.synchronize()
            start = time.perf_counter()
            model = model.to("cuda:0")
            torch.cuda.synchronize()
            remote_restore_time = time.perf_counter() - start
            remote_restore_times.append(remote_restore_time)

            del model
            gc.collect()
            torch.cuda.empty_cache()

        local_avg_ms = (sum(local_restore_times) / len(local_restore_times)) * 1000
        remote_avg_ms = (sum(remote_restore_times) / len(remote_restore_times)) * 1000
        penalty_pct = ((remote_avg_ms - local_avg_ms) / local_avg_ms) * 100

        print(f"\n{'='*70}")
        print(f"MODEL CACHE RESTORE (from_cache) - Cross-NUMA Penalty")
        print(f"{'='*70}")
        print(f"Model:            {benchmark_model}")
        print(f"Model size:       {model_size_mb:.0f} MB")
        print(f"GPU:              {gpu_idx} (NUMA node {local_node})")
        print(f"Remote NUMA node: {remote_node}")
        print(f"Iterations:       {benchmark_iterations}")
        print(f"")
        print(f"ALIGNED (cache on GPU's NUMA node):")
        print(f"  Restore time:   {local_avg_ms:.2f} ms")
        print(f"")
        print(f"MISALIGNED (cache on remote NUMA node):")
        print(f"  Restore time:   {remote_avg_ms:.2f} ms")
        print(f"")
        print(f">>> CROSS-NUMA PENALTY: {penalty_pct:+.1f}% <<<")
        print(f"{'='*70}")

    @skip_without_gpu
    @skip_without_numa
    @skip_without_libnuma
    @pytest.mark.benchmark
    def test_to_cache_cross_numa(self, benchmark_model, benchmark_iterations):
        """Measure to_cache time: local NUMA vs remote NUMA.

        Measures the penalty when offloading a model to a remote NUMA node.
        """
        import torch
        from transformers import AutoModelForCausalLM

        pair = _find_cross_numa_pair()
        if pair is None:
            pytest.skip("Cannot find GPU with cross-NUMA pair")

        gpu_idx, local_node, remote_node = pair
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_idx)

        local_offload_times = []
        remote_offload_times = []

        for iteration in range(benchmark_iterations):
            gc.collect()
            torch.cuda.empty_cache()

            # ============================================================
            # LOCAL NUMA: Offload to local NUMA node
            # ============================================================
            _set_memory_policy_preferred(local_node)

            model = AutoModelForCausalLM.from_pretrained(
                benchmark_model,
                torch_dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            model.requires_grad_(False)
            torch.cuda.synchronize()

            if iteration == 0:
                model_size_mb = _get_model_size_mb(model)

            torch.cuda.synchronize()
            start = time.perf_counter()
            model = model.cpu()
            local_offload_time = time.perf_counter() - start
            local_offload_times.append(local_offload_time)

            del model
            gc.collect()
            torch.cuda.empty_cache()

            # ============================================================
            # REMOTE NUMA: Offload to remote NUMA node
            # ============================================================
            _set_memory_policy_preferred(remote_node)

            model = AutoModelForCausalLM.from_pretrained(
                benchmark_model,
                torch_dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            model.requires_grad_(False)
            torch.cuda.synchronize()

            torch.cuda.synchronize()
            start = time.perf_counter()
            model = model.cpu()
            remote_offload_time = time.perf_counter() - start
            remote_offload_times.append(remote_offload_time)

            del model
            gc.collect()
            torch.cuda.empty_cache()

        local_avg_ms = (sum(local_offload_times) / len(local_offload_times)) * 1000
        remote_avg_ms = (sum(remote_offload_times) / len(remote_offload_times)) * 1000
        penalty_pct = ((remote_avg_ms - local_avg_ms) / local_avg_ms) * 100

        print(f"\n{'='*70}")
        print(f"MODEL CACHE OFFLOAD (to_cache) - Cross-NUMA Penalty")
        print(f"{'='*70}")
        print(f"Model:            {benchmark_model}")
        print(f"Model size:       {model_size_mb:.0f} MB")
        print(f"GPU:              {gpu_idx} (NUMA node {local_node})")
        print(f"Remote NUMA node: {remote_node}")
        print(f"Iterations:       {benchmark_iterations}")
        print(f"")
        print(f"ALIGNED (offload to GPU's NUMA node):")
        print(f"  Offload time:   {local_avg_ms:.2f} ms")
        print(f"")
        print(f"MISALIGNED (offload to remote NUMA node):")
        print(f"  Offload time:   {remote_avg_ms:.2f} ms")
        print(f"")
        print(f">>> CROSS-NUMA PENALTY: {penalty_pct:+.1f}% <<<")
        print(f"{'='*70}")

    @skip_without_gpu
    @skip_without_numa
    @skip_without_libnuma
    @pytest.mark.benchmark
    def test_full_cache_cycle_cross_numa(self, benchmark_model, benchmark_iterations):
        """Measure full to_cache + from_cache cycle with cross-NUMA penalty.

        This measures the total penalty for a complete cache swap operation.
        """
        import torch
        from transformers import AutoModelForCausalLM

        pair = _find_cross_numa_pair()
        if pair is None:
            pytest.skip("Cannot find GPU with cross-NUMA pair")

        gpu_idx, local_node, remote_node = pair
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_idx)

        local_cycle_times = []
        remote_cycle_times = []

        for iteration in range(benchmark_iterations):
            gc.collect()
            torch.cuda.empty_cache()

            # ============================================================
            # LOCAL NUMA: Full cycle on local NUMA
            # ============================================================
            _set_memory_policy_preferred(local_node)

            model = AutoModelForCausalLM.from_pretrained(
                benchmark_model,
                torch_dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            model.requires_grad_(False)
            torch.cuda.synchronize()

            if iteration == 0:
                model_size_mb = _get_model_size_mb(model)

            # Full cycle: GPU -> CPU -> GPU
            torch.cuda.synchronize()
            start = time.perf_counter()
            model = model.cpu()
            model = model.to("cuda:0")
            torch.cuda.synchronize()
            local_cycle_time = time.perf_counter() - start
            local_cycle_times.append(local_cycle_time)

            del model
            gc.collect()
            torch.cuda.empty_cache()

            # ============================================================
            # REMOTE NUMA: Full cycle with cache on remote NUMA
            # ============================================================
            _set_memory_policy_preferred(remote_node)

            model = AutoModelForCausalLM.from_pretrained(
                benchmark_model,
                torch_dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            model.requires_grad_(False)
            torch.cuda.synchronize()

            torch.cuda.synchronize()
            start = time.perf_counter()
            model = model.cpu()
            model = model.to("cuda:0")
            torch.cuda.synchronize()
            remote_cycle_time = time.perf_counter() - start
            remote_cycle_times.append(remote_cycle_time)

            del model
            gc.collect()
            torch.cuda.empty_cache()

        local_avg_ms = (sum(local_cycle_times) / len(local_cycle_times)) * 1000
        remote_avg_ms = (sum(remote_cycle_times) / len(remote_cycle_times)) * 1000
        penalty_pct = ((remote_avg_ms - local_avg_ms) / local_avg_ms) * 100

        print(f"\n{'='*70}")
        print(f"FULL CACHE CYCLE (to_cache + from_cache) - Cross-NUMA Penalty")
        print(f"{'='*70}")
        print(f"Model:            {benchmark_model}")
        print(f"Model size:       {model_size_mb:.0f} MB")
        print(f"GPU:              {gpu_idx} (NUMA node {local_node})")
        print(f"Remote NUMA node: {remote_node}")
        print(f"Iterations:       {benchmark_iterations}")
        print(f"")
        print(f"ALIGNED (cache on GPU's NUMA node):")
        print(f"  Cycle time:     {local_avg_ms:.2f} ms")
        print(f"")
        print(f"MISALIGNED (cache on remote NUMA node):")
        print(f"  Cycle time:     {remote_avg_ms:.2f} ms")
        print(f"")
        print(f">>> CROSS-NUMA PENALTY: {penalty_pct:+.1f}% <<<")
        print(f"{'='*70}")


class TestModelLoadCrossNuma:
    """Benchmark model loading with cross-NUMA CPU affinity."""

    @skip_without_gpu
    @skip_without_numa
    @skip_without_libnuma
    @pytest.mark.benchmark
    def test_model_load_cross_numa(self, benchmark_model, benchmark_iterations):
        """Measure model loading time with different NUMA affinities.

        Tests the penalty when the process has CPU affinity to a remote
        NUMA node while loading a model to a GPU.

        Includes warmup phase to populate page cache before measurement.
        """
        import torch
        from transformers import AutoModelForCausalLM

        pair = _find_cross_numa_pair()
        if pair is None:
            pytest.skip("Cannot find GPU with cross-NUMA pair")

        gpu_idx, local_node, remote_node = pair
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_idx)

        # ============================================================
        # WARMUP: Load model once to populate page cache
        # ============================================================
        print(f"\nWarming up page cache...")
        model = AutoModelForCausalLM.from_pretrained(
            benchmark_model,
            torch_dtype=torch.bfloat16,
            device_map="cpu",  # Load to CPU only for warmup
        )
        model_size_mb = _get_model_size_mb(model)
        del model
        gc.collect()
        torch.cuda.empty_cache()
        print(f"Page cache warmed. Model size: {model_size_mb:.0f} MB")

        local_load_times = []
        remote_load_times = []

        for iteration in range(benchmark_iterations):
            gc.collect()
            torch.cuda.empty_cache()

            # ============================================================
            # LOCAL NUMA: Load with CPU affinity to GPU's NUMA node
            # ============================================================
            _set_memory_policy_preferred(local_node)
            _set_cpu_affinity_to_node(local_node)

            start = time.perf_counter()
            model = AutoModelForCausalLM.from_pretrained(
                benchmark_model,
                torch_dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            torch.cuda.synchronize()
            local_load_time = time.perf_counter() - start
            local_load_times.append(local_load_time)

            del model
            gc.collect()
            torch.cuda.empty_cache()

            # ============================================================
            # REMOTE NUMA: Load with CPU affinity to remote NUMA node
            # ============================================================
            _set_memory_policy_preferred(remote_node)
            _set_cpu_affinity_to_node(remote_node)

            start = time.perf_counter()
            model = AutoModelForCausalLM.from_pretrained(
                benchmark_model,
                torch_dtype=torch.bfloat16,
                device_map="cuda:0",
            )
            torch.cuda.synchronize()
            remote_load_time = time.perf_counter() - start
            remote_load_times.append(remote_load_time)

            del model
            gc.collect()
            torch.cuda.empty_cache()

        local_avg_s = sum(local_load_times) / len(local_load_times)
        remote_avg_s = sum(remote_load_times) / len(remote_load_times)
        penalty_pct = ((remote_avg_s - local_avg_s) / local_avg_s) * 100

        print(f"\n{'='*70}")
        print(f"MODEL LOADING (page cache warmed) - Cross-NUMA Penalty")
        print(f"{'='*70}")
        print(f"Model:            {benchmark_model}")
        print(f"Model size:       {model_size_mb:.0f} MB")
        print(f"GPU:              {gpu_idx} (NUMA node {local_node})")
        print(f"Remote NUMA node: {remote_node}")
        print(f"Iterations:       {benchmark_iterations}")
        print(f"")
        print(f"ALIGNED (CPU affinity to GPU's NUMA node):")
        print(f"  Load time:      {local_avg_s:.2f} s")
        print(f"")
        print(f"MISALIGNED (CPU affinity to remote NUMA node):")
        print(f"  Load time:      {remote_avg_s:.2f} s")
        print(f"")
        print(f">>> CROSS-NUMA PENALTY: {penalty_pct:+.1f}% <<<")
        print(f"{'='*70}")


class TestSummary:
    """Print system NUMA topology summary."""

    @skip_without_numa
    @pytest.mark.benchmark
    def test_print_numa_topology(self):
        """Print NUMA topology and GPU mapping for reference."""
        import torch

        print(f"\n{'='*70}")
        print(f"SYSTEM NUMA TOPOLOGY")
        print(f"{'='*70}")

        numa_count = _get_numa_node_count()
        print(f"NUMA nodes: {numa_count}")

        for node_id in range(numa_count):
            cpulist_path = Path(f"/sys/devices/system/node/node{node_id}/cpulist")
            meminfo_path = Path(f"/sys/devices/system/node/node{node_id}/meminfo")

            cpus = "unknown"
            if cpulist_path.exists():
                cpus = cpulist_path.read_text().strip()

            mem_gb = 0
            if meminfo_path.exists():
                for line in meminfo_path.read_text().splitlines():
                    if "MemTotal" in line:
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part.isdigit() and i + 1 < len(parts) and parts[i + 1] == "kB":
                                mem_gb = int(part) / (1024 * 1024)
                                break
                        break

            print(f"  Node {node_id}: CPUs [{cpus}], Memory: {mem_gb:.0f} GB")

        print(f"")
        print(f"GPU-to-NUMA Mapping:")

        try:
            gpu_count = torch.cuda.device_count()
            for gpu_idx in range(gpu_count):
                numa_node = _get_gpu_numa_node(gpu_idx)
                gpu_name = torch.cuda.get_device_name(gpu_idx)
                if numa_node is not None:
                    print(f"  GPU {gpu_idx}: {gpu_name} -> NUMA node {numa_node}")
                else:
                    print(f"  GPU {gpu_idx}: {gpu_name} -> NUMA node unknown")
        except Exception as e:
            print(f"  Could not determine GPU mapping: {e}")

        print(f"{'='*70}")
