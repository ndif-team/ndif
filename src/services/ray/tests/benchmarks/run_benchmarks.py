#!/usr/bin/env python3
"""CLI runner for cross-NUMA penalty benchmarks.

Usage:
    python run_benchmarks.py                    # Run all benchmarks
    python run_benchmarks.py --quick            # Quick mode (3 iterations)
    python run_benchmarks.py --memory           # Memory transfer benchmarks only
    python run_benchmarks.py --model            # Model loading benchmarks only
    python run_benchmarks.py --model gpt2-large # Use a specific model
    python run_benchmarks.py --info             # Print system info only
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def get_system_info() -> dict:
    """Collect system information for benchmark context."""
    import platform

    info = {
        "platform": platform.system(),
        "python_version": platform.python_version(),
        "timestamp": datetime.now().isoformat(),
    }

    # NUMA info
    numa_nodes = Path("/sys/devices/system/node")
    if numa_nodes.exists():
        nodes = list(numa_nodes.glob("node[0-9]*"))
        info["numa_nodes"] = len(nodes)

        # Get memory per node
        node_memory = {}
        for node in sorted(nodes):
            meminfo = node / "meminfo"
            if meminfo.exists():
                content = meminfo.read_text()
                for line in content.splitlines():
                    if "MemTotal" in line:
                        parts = line.split()
                        for i, part in enumerate(parts):
                            if part.isdigit() and i + 1 < len(parts) and parts[i + 1] == "kB":
                                mem_gb = int(part) / (1024 * 1024)
                                node_memory[node.name] = f"{mem_gb:.0f} GB"
                                break
                        break
        info["numa_memory"] = node_memory

    # GPU info
    try:
        import torch
        if torch.cuda.is_available():
            info["gpu_count"] = torch.cuda.device_count()
            info["gpu_names"] = [
                torch.cuda.get_device_name(i)
                for i in range(torch.cuda.device_count())
            ]

            # GPU to NUMA mapping using nvidia-smi
            gpu_numa = {}
            try:
                for i in range(torch.cuda.device_count()):
                    result = subprocess.run(
                        ["nvidia-smi", "--query-gpu=pci.bus_id", "--format=csv,noheader", f"--id={i}"],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        pci_bus_id = result.stdout.strip().lower()
                        for pci_id in [pci_bus_id, pci_bus_id.replace("00000000:", "0000:")]:
                            numa_path = Path(f"/sys/bus/pci/devices/{pci_id}/numa_node")
                            if numa_path.exists():
                                node_id = int(numa_path.read_text().strip())
                                gpu_numa[f"GPU {i}"] = node_id if node_id >= 0 else "unknown"
                                break
                        else:
                            gpu_numa[f"GPU {i}"] = "unknown"
                info["gpu_numa_mapping"] = gpu_numa
            except Exception:
                pass
    except ImportError:
        info["gpu_count"] = 0

    # libnuma availability
    try:
        import ctypes
        import ctypes.util

        lib_path = ctypes.util.find_library("numa")
        if lib_path:
            lib = ctypes.CDLL(lib_path)
            info["libnuma_available"] = lib.numa_available() >= 0
        else:
            info["libnuma_available"] = False
    except Exception:
        info["libnuma_available"] = False

    return info


def print_system_info(info: dict):
    """Pretty print system info."""
    print(f"\n{'='*60}")
    print("SYSTEM INFORMATION")
    print(f"{'='*60}")
    print(f"Platform:     {info.get('platform', 'unknown')}")
    print(f"Python:       {info.get('python_version', 'unknown')}")
    print(f"NUMA nodes:   {info.get('numa_nodes', 'N/A')}")
    print(f"libnuma:      {'yes' if info.get('libnuma_available') else 'no'}")

    if info.get('numa_memory'):
        print(f"")
        print("NUMA Memory:")
        for node, mem in sorted(info['numa_memory'].items()):
            print(f"  {node}: {mem}")

    print(f"")
    print(f"GPUs:         {info.get('gpu_count', 0)}")
    if info.get('gpu_names'):
        for i, name in enumerate(info['gpu_names']):
            numa = info.get('gpu_numa_mapping', {}).get(f"GPU {i}", "unknown")
            print(f"  GPU {i}: {name} (NUMA node {numa})")

    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(
        description="Run cross-NUMA penalty benchmarks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--memory",
        action="store_true",
        help="Run memory transfer benchmarks only",
    )
    parser.add_argument(
        "--model",
        action="store_true",
        help="Run model loading benchmarks only",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Quick mode (3 iterations instead of 10)",
    )
    parser.add_argument(
        "--benchmark-model",
        type=str,
        default="openai-community/gpt2",
        help="Model to use for benchmarks (default: openai-community/gpt2)",
    )
    parser.add_argument(
        "--json",
        type=str,
        metavar="FILE",
        help="Save results to JSON file",
    )
    parser.add_argument(
        "--info",
        action="store_true",
        help="Print system info and exit",
    )

    args = parser.parse_args()

    # Get and print system info
    info = get_system_info()
    print_system_info(info)

    if args.info:
        if args.json:
            with open(args.json, "w") as f:
                json.dump(info, f, indent=2)
            print(f"\nSystem info saved to {args.json}")
        return 0

    # Check requirements
    if info.get('numa_nodes', 0) < 2:
        print("\nERROR: This benchmark requires at least 2 NUMA nodes.")
        print("Your system has a single NUMA node or NUMA is not available.")
        return 1

    if not info.get('libnuma_available'):
        print("\nERROR: libnuma is not available.")
        print("Install with: apt-get install libnuma-dev")
        return 1

    if info.get('gpu_count', 0) == 0:
        print("\nERROR: No GPUs detected.")
        return 1

    # Build pytest command
    tests_dir = Path(__file__).parent

    cmd = [
        sys.executable, "-m", "pytest",
        "-v",
        "-s",  # Show print output
        "-m", "benchmark",
    ]

    if args.quick:
        cmd.append("--quick")

    cmd.extend(["--benchmark-model", args.benchmark_model])

    # Select test files
    if args.memory:
        cmd.append(str(tests_dir / "bench_numa_memory.py"))
    elif args.model:
        cmd.append(str(tests_dir / "bench_numa_model_load.py"))
    else:
        # Run all benchmarks
        cmd.append(str(tests_dir))

    print(f"\n{'='*60}")
    print("RUNNING BENCHMARKS")
    print(f"{'='*60}")
    print(f"Command: {' '.join(cmd)}")
    print(f"Model:   {args.benchmark_model}")
    print(f"Mode:    {'quick (3 iterations)' if args.quick else 'full (10 iterations)'}")
    print(f"{'='*60}\n")

    result = subprocess.call(cmd)

    if args.json:
        results = {
            "system_info": info,
            "benchmark_model": args.benchmark_model,
            "quick_mode": args.quick,
            "exit_code": result,
        }
        with open(args.json, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nResults saved to {args.json}")

    return result


if __name__ == "__main__":
    sys.exit(main())
