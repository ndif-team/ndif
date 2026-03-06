"""Benchmark: Model loading from disk with varying parallelism strategies.

Measures wall-clock time for loading HuggingFace models under different
configurations to identify the optimal strategy for Lustre / NVMe-oF storage
(e.g., NCSA Delta).

Experiments:
  baseline            - standard from_pretrained (v5 default, 4 worker threads)
  workers             - from_pretrained with GLOBAL_WORKERS = 1,2,4,8,16,32
  prefetch            - warm page cache first, then from_pretrained
  parallel_shards     - thread-pool load_file() bypass with v5 optimizations

Page cache invalidation between experiments:
  By default, creates a large file (512GB) on /tmp at startup and reads it
  between experiments to pressure the kernel into evicting model shard pages.
  Local /tmp on Delta GPU nodes is 1.5TB NVMe, so 512GB is safe.

  With --sudo-drop-caches, uses 'sync && echo 3 > /proc/sys/vm/drop_caches'
  instead (requires root). This is faster and 100% reliable. Useful when
  Delta engineers run the benchmark for us.

Usage:
  # Default: creates 512GB junk file on /tmp for cache invalidation
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B

  # With root access (faster, reliable cache drop)
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --sudo-drop-caches

  # Custom GPU set
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --gpus 0,1

  # Specific experiments only
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --experiments baseline workers

  # Repeat each experiment N times
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --repeats 3
"""

import argparse
import gc
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import torch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class TimingResult:
    experiment: str
    config: dict
    wall_time_s: float
    phase_times_s: dict = field(default_factory=dict)
    peak_gpu_mem_mb: float = 0.0
    peak_cpu_mem_mb: float = 0.0
    error: Optional[str] = None


def get_process_rss_mb() -> float:
    """Current process RSS in MB via /proc/self/status."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024  # kB -> MB
    except Exception:
        pass
    return 0.0


def get_gpu_mem_allocated_mb(gpu_ids: list[int]) -> float:
    return sum(torch.cuda.memory_allocated(i) / (1024 ** 2) for i in gpu_ids)


def get_gpu_mem_reserved_mb(gpu_ids: list[int]) -> float:
    return sum(torch.cuda.memory_reserved(i) / (1024 ** 2) for i in gpu_ids)


# ---------------------------------------------------------------------------
# Page cache invalidation
# ---------------------------------------------------------------------------

JUNK_FILE_PATH = "/tmp/bench_disk_loading_junk.bin"
JUNK_FILE_SIZE_GB = 512


def create_junk_file(path: str = JUNK_FILE_PATH, size_gb: int = JUNK_FILE_SIZE_GB):
    """Create a large junk file on local /tmp for page cache invalidation.

    Uses dd with /dev/zero for speed. On Delta, /tmp is 1.5TB local NVMe
    so 512GB is safe. Writing zeros is fast (~2-3 GB/s) and the content
    doesn't matter — we just need to fill the page cache with junk.
    """
    if os.path.exists(path):
        existing_gb = os.path.getsize(path) / (1024 ** 3)
        if existing_gb >= size_gb * 0.99:
            print(f"  [junk] Reusing existing {existing_gb:.0f} GB file at {path}")
            return
        print(f"  [junk] Existing file too small ({existing_gb:.0f} GB), recreating...")

    print(f"  [junk] Creating {size_gb} GB junk file at {path}...")
    t0 = time.perf_counter()
    subprocess.run(
        ["dd", "if=/dev/zero", f"of={path}", "bs=1M", f"count={size_gb * 1024}",
         "status=progress"],
        check=True,
    )
    elapsed = time.perf_counter() - t0
    print(f"  [junk] Created in {elapsed:.0f}s ({size_gb / elapsed:.1f} GB/s)")


def delete_junk_file_background(path: str = JUNK_FILE_PATH):
    """Delete the junk file in the background (non-blocking)."""
    if not os.path.exists(path):
        return
    print(f"  [junk] Deleting {path} in background...")
    subprocess.Popen(["rm", "-f", path])


def sudo_drop_caches():
    """Drop page caches via /proc/sys/vm/drop_caches. Requires root."""
    print("  [cache] Dropping page caches (sudo)...")
    t0 = time.perf_counter()
    subprocess.run(
        ["sudo", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches"],
        check=True,
        timeout=30,
    )
    elapsed = time.perf_counter() - t0
    print(f"  [cache] Done in {elapsed:.1f}s")


def invalidate_via_junk_file(path: str):
    """Read the junk file to pressure model pages out of the page cache."""
    if not os.path.exists(path):
        print(f"  [cache] Junk file not found: {path}")
        return

    size_gb = os.path.getsize(path) / (1024 ** 3)
    print(f"  [cache] Reading {size_gb:.0f} GB junk file to evict page cache...")
    t0 = time.perf_counter()
    with open(path, "rb") as f:
        while f.read(16 * 1024 * 1024):  # 16MB chunks
            pass
    elapsed = time.perf_counter() - t0
    bw = size_gb / elapsed if elapsed > 0 else 0
    print(f"  [cache] Done in {elapsed:.1f}s ({bw:.1f} GB/s)")


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------

def resolve_shard_paths(model_id: str, revision: str = "main") -> list[str]:
    """Resolve local shard file paths from the HF cache without downloading."""
    from huggingface_hub import snapshot_download

    model_dir = snapshot_download(model_id, revision=revision, local_files_only=True)
    shard_paths = sorted(Path(model_dir).glob("*.safetensors"))
    if not shard_paths:
        shard_paths = sorted(Path(model_dir).glob("*.bin"))
    return [str(p) for p in shard_paths]


def warm_page_cache(shard_paths: list[str], num_threads: int = 8):
    """Read shard files into OS page cache using parallel threads."""
    def _read_file(path: str):
        with open(path, "rb") as f:
            while f.read(8 * 1024 * 1024):  # 8MB chunks
                pass

    with ThreadPoolExecutor(max_workers=min(num_threads, len(shard_paths))) as pool:
        list(pool.map(_read_file, shard_paths))


def unload_model(model):
    """Fully unload a model and free GPU memory."""
    if model is not None:
        del model
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()


@contextmanager
def patch_global_workers(n_workers: int):
    """Monkey-patch transformers.core_model_loading.GLOBAL_WORKERS."""
    import transformers.core_model_loading as cml

    original = cml.GLOBAL_WORKERS
    cml.GLOBAL_WORKERS = n_workers
    try:
        yield
    finally:
        cml.GLOBAL_WORKERS = original


def build_max_memory(gpu_ids: list[int]) -> dict:
    """Build a max_memory dict that restricts placement to the given GPUs."""
    max_memory = {}
    for i in range(torch.cuda.device_count()):
        if i in gpu_ids:
            mem = torch.cuda.get_device_properties(i).total_mem
            max_memory[i] = int(mem * 0.9)  # 90% of GPU memory
        else:
            max_memory[i] = 0
    return max_memory


# ---------------------------------------------------------------------------
# Experiment: Baseline from_pretrained
# ---------------------------------------------------------------------------

def run_baseline(model_id: str, gpu_ids: list[int], dtype: torch.dtype,
                 revision: str = "main") -> TimingResult:
    """Standard from_pretrained with v5 defaults (4 worker threads)."""
    from transformers import AutoModelForCausalLM

    max_memory = build_max_memory(gpu_ids)

    torch.cuda.synchronize()
    t0 = time.perf_counter()

    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        revision=revision,
        device_map="auto",
        max_memory=max_memory,
        torch_dtype=dtype,
        attn_implementation="eager",
    )

    torch.cuda.synchronize()
    wall = time.perf_counter() - t0

    peak_gpu = get_gpu_mem_allocated_mb(gpu_ids)
    peak_cpu = get_process_rss_mb()

    unload_model(model)

    return TimingResult(
        experiment="baseline",
        config={"workers": 4},
        wall_time_s=wall,
        peak_gpu_mem_mb=peak_gpu,
        peak_cpu_mem_mb=peak_cpu,
    )


# ---------------------------------------------------------------------------
# Experiment: Varying worker count
# ---------------------------------------------------------------------------

def run_workers(model_id: str, gpu_ids: list[int], dtype: torch.dtype,
                n_workers: int, revision: str = "main") -> TimingResult:
    """from_pretrained with a custom GLOBAL_WORKERS count."""
    from transformers import AutoModelForCausalLM

    max_memory = build_max_memory(gpu_ids)

    with patch_global_workers(n_workers):
        torch.cuda.synchronize()
        t0 = time.perf_counter()

        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            revision=revision,
            device_map="auto",
            max_memory=max_memory,
            torch_dtype=dtype,
            attn_implementation="eager",
        )

        torch.cuda.synchronize()
        wall = time.perf_counter() - t0

    peak_gpu = get_gpu_mem_allocated_mb(gpu_ids)
    peak_cpu = get_process_rss_mb()

    unload_model(model)

    return TimingResult(
        experiment="workers",
        config={"workers": n_workers},
        wall_time_s=wall,
        peak_gpu_mem_mb=peak_gpu,
        peak_cpu_mem_mb=peak_cpu,
    )


# ---------------------------------------------------------------------------
# Experiment: Page cache warming + from_pretrained
# ---------------------------------------------------------------------------

def run_prefetch(model_id: str, gpu_ids: list[int], dtype: torch.dtype,
                 prefetch_threads: int = 8, revision: str = "main") -> TimingResult:
    """Warm page cache by pre-reading all shard files, then from_pretrained."""
    from transformers import AutoModelForCausalLM

    max_memory = build_max_memory(gpu_ids)
    phases = {}

    shard_paths = resolve_shard_paths(model_id, revision)
    total_size_mb = sum(os.path.getsize(p) for p in shard_paths) / (1024 ** 2)

    # Phase 1: Prefetch
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    warm_page_cache(shard_paths, num_threads=prefetch_threads)
    t_prefetch = time.perf_counter() - t0
    phases["prefetch"] = t_prefetch
    prefetch_bw = total_size_mb / t_prefetch / 1024 if t_prefetch > 0 else 0
    print(f"  [prefetch] {len(shard_paths)} shards, {total_size_mb:.0f} MB "
          f"in {t_prefetch:.2f}s ({prefetch_bw:.1f} GB/s)")

    # Phase 2: Load (should hit page cache)
    t1 = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        revision=revision,
        device_map="auto",
        max_memory=max_memory,
        torch_dtype=dtype,
        attn_implementation="eager",
    )
    torch.cuda.synchronize()
    t_load = time.perf_counter() - t1
    phases["load_after_prefetch"] = t_load

    wall = time.perf_counter() - t0
    peak_gpu = get_gpu_mem_allocated_mb(gpu_ids)
    peak_cpu = get_process_rss_mb()

    unload_model(model)

    return TimingResult(
        experiment="prefetch",
        config={"prefetch_threads": prefetch_threads, "shard_count": len(shard_paths),
                "total_size_mb": round(total_size_mb, 1)},
        wall_time_s=wall,
        phase_times_s=phases,
        peak_gpu_mem_mb=peak_gpu,
        peak_cpu_mem_mb=peak_cpu,
    )


# ---------------------------------------------------------------------------
# Experiment: Parallel shard loading (bypass from_pretrained for I/O)
# ---------------------------------------------------------------------------

def run_parallel_shards(model_id: str, gpu_ids: list[int], dtype: torch.dtype,
                        n_workers: int = 8, revision: str = "main") -> TimingResult:
    """Load all safetensors shards in parallel via thread pool, then dispatch.

    This bypasses from_pretrained's sequential shard opening but replicates
    the key v5 optimizations:
      - Meta device init (no double memory)
      - CUDA allocator warmup
      - accelerate dispatch for device placement
    """
    from transformers import AutoModelForCausalLM, AutoConfig
    from safetensors.torch import load_file
    from accelerate import init_empty_weights, infer_auto_device_map, dispatch_model

    max_memory = build_max_memory(gpu_ids)
    phases = {}

    shard_paths = resolve_shard_paths(model_id, revision)
    total_size_mb = sum(os.path.getsize(p) for p in shard_paths) / (1024 ** 2)

    if not shard_paths or not shard_paths[0].endswith(".safetensors"):
        return TimingResult(
            experiment="parallel_shards",
            config={"workers": n_workers},
            wall_time_s=0,
            error="No safetensors files found, skipping",
        )

    torch.cuda.synchronize()
    t0 = time.perf_counter()

    # Phase 1: Build model architecture on meta device
    t_meta_start = time.perf_counter()
    config = AutoConfig.from_pretrained(model_id, revision=revision)
    with init_empty_weights():
        model = AutoModelForCausalLM.from_config(config, attn_implementation="eager")
    phases["meta_init"] = time.perf_counter() - t_meta_start

    # Phase 2: Compute device map
    t_map_start = time.perf_counter()
    device_map = infer_auto_device_map(model, max_memory=max_memory, dtype=dtype)
    phases["device_map"] = time.perf_counter() - t_map_start

    # Phase 3: CUDA allocator warmup (replicate v5 optimization)
    t_warmup_start = time.perf_counter()
    try:
        from transformers.modeling_utils import caching_allocator_warmup, expand_device_map
        expected_keys = list(model.state_dict().keys())
        expanded = expand_device_map(device_map, expected_keys)
        caching_allocator_warmup(model, expanded, None)
        phases["cuda_warmup"] = time.perf_counter() - t_warmup_start
    except Exception as e:
        phases["cuda_warmup"] = time.perf_counter() - t_warmup_start
        print(f"  [warn] Could not run caching_allocator_warmup: {e}")

    # Phase 4: Parallel shard loading
    t_io_start = time.perf_counter()
    effective_workers = min(n_workers, len(shard_paths))
    with ThreadPoolExecutor(max_workers=effective_workers) as pool:
        state_dicts = list(pool.map(load_file, shard_paths))
    merged = {}
    for sd in state_dicts:
        merged.update(sd)
    del state_dicts
    t_io = time.perf_counter() - t_io_start
    phases["parallel_io"] = t_io
    io_bw = total_size_mb / t_io / 1024 if t_io > 0 else 0
    print(f"  [parallel_io] {len(shard_paths)} shards, {total_size_mb:.0f} MB "
          f"in {t_io:.2f}s ({io_bw:.1f} GB/s) with {effective_workers} workers")

    # Phase 5: Load state dict and dispatch to GPUs
    t_dispatch_start = time.perf_counter()

    # Cast dtype before loading to avoid double memory from in-place conversion
    if dtype is not None:
        for k, v in merged.items():
            if v.is_floating_point() and v.dtype != dtype:
                merged[k] = v.to(dtype=dtype)

    model.load_state_dict(merged, assign=True)
    del merged
    gc.collect()

    model = dispatch_model(model, device_map=device_map)
    torch.cuda.synchronize()
    phases["dispatch"] = time.perf_counter() - t_dispatch_start

    wall = time.perf_counter() - t0

    peak_gpu = get_gpu_mem_allocated_mb(gpu_ids)
    peak_cpu = get_process_rss_mb()

    unload_model(model)

    return TimingResult(
        experiment="parallel_shards",
        config={"workers": n_workers, "shard_count": len(shard_paths),
                "total_size_mb": round(total_size_mb, 1)},
        wall_time_s=wall,
        phase_times_s=phases,
        peak_gpu_mem_mb=peak_gpu,
        peak_cpu_mem_mb=peak_cpu,
    )


# ---------------------------------------------------------------------------
# Experiment registry & dispatch
# ---------------------------------------------------------------------------

EXPERIMENT_CONFIGS = {
    "baseline": [("baseline", {"workers": 4})],
    "workers": [("workers", {"workers": n}) for n in [1, 2, 4, 8, 16, 32]],
    "prefetch": [("prefetch", {"prefetch_threads": t}) for t in [4, 8, 16]],
    "parallel_shards": [("parallel_shards", {"workers": n}) for n in [4, 8, 16]],
}


def run_single_config(exp_name: str, config: dict, model_id: str, gpu_ids: list[int],
                      dtype: torch.dtype, revision: str = "main") -> TimingResult:
    if exp_name == "baseline":
        return run_baseline(model_id, gpu_ids, dtype, revision)
    elif exp_name == "workers":
        return run_workers(model_id, gpu_ids, dtype, config["workers"], revision)
    elif exp_name == "prefetch":
        return run_prefetch(model_id, gpu_ids, dtype, config["prefetch_threads"], revision)
    elif exp_name == "parallel_shards":
        return run_parallel_shards(model_id, gpu_ids, dtype, config["workers"], revision)
    else:
        raise ValueError(f"Unknown experiment: {exp_name}")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_result(r: TimingResult):
    status = "ERROR" if r.error else "OK"
    print(f"\n  [{status}] {r.experiment} | config={r.config}")
    if r.error:
        print(f"    error: {r.error}")
        return
    print(f"    wall_time:    {r.wall_time_s:.2f}s")
    if r.phase_times_s:
        for phase, t in r.phase_times_s.items():
            print(f"    {phase}: {t:.2f}s")
    print(f"    peak_gpu_mem: {r.peak_gpu_mem_mb:.0f} MB")
    print(f"    peak_cpu_mem: {r.peak_cpu_mem_mb:.0f} MB")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Benchmark model loading strategies")
    parser.add_argument("--model", default="Qwen/Qwen2.5-72B",
                        help="HuggingFace model ID (default: Qwen/Qwen2.5-72B, ~144GB bf16, fits 4x A40)")
    parser.add_argument("--revision", default="main", help="Model revision")
    parser.add_argument("--gpus", default=None, help="Comma-separated GPU IDs (default: all)")
    parser.add_argument("--dtype", default="bfloat16", choices=["bfloat16", "float16", "float32"],
                        help="Model dtype")
    parser.add_argument("--experiments", nargs="+", default=list(EXPERIMENT_CONFIGS.keys()),
                        choices=list(EXPERIMENT_CONFIGS.keys()), help="Which experiments to run")
    parser.add_argument("--repeats", type=int, default=1, help="Repeat each experiment N times")
    parser.add_argument("--sudo-drop-caches", action="store_true",
                        help="Use 'sudo echo 3 > /proc/sys/vm/drop_caches' (requires root, fast & reliable)")
    parser.add_argument("--output", default=None, help="JSON output file path")

    args = parser.parse_args()

    dtype_map = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}
    dtype = dtype_map[args.dtype]

    if args.gpus:
        gpu_ids = [int(x) for x in args.gpus.split(",")]
    else:
        gpu_ids = list(range(torch.cuda.device_count()))

    print(f"Model:       {args.model}")
    print(f"GPUs:        {gpu_ids}")
    print(f"Dtype:       {args.dtype}")
    print(f"Repeats:     {args.repeats}")
    print(f"Experiments: {args.experiments}")

    # --- Cache invalidation setup ---
    use_sudo = args.sudo_drop_caches
    junk_file = None

    if use_sudo:
        print("Cache mode:  sudo drop_caches")
        # Verify sudo works
        try:
            subprocess.run(["sudo", "-n", "true"], check=True, timeout=5,
                           capture_output=True)
        except Exception:
            print("ERROR: --sudo-drop-caches requires passwordless sudo. Exiting.")
            sys.exit(1)
    else:
        print(f"Cache mode:  junk file ({JUNK_FILE_SIZE_GB} GB on /tmp)")
        junk_file = JUNK_FILE_PATH
        create_junk_file(junk_file, JUNK_FILE_SIZE_GB)

    def drop_caches():
        if use_sudo:
            sudo_drop_caches()
        else:
            invalidate_via_junk_file(junk_file)

    # --- Ensure model is downloaded ---
    print("\nEnsuring model is in local cache...")
    try:
        shard_paths = resolve_shard_paths(args.model, args.revision)
        total_mb = sum(os.path.getsize(p) for p in shard_paths) / (1024 ** 2)
        print(f"  Found {len(shard_paths)} shards, {total_mb:.0f} MB total")
    except Exception:
        print("  Model not in local cache. Downloading...")
        from huggingface_hub import snapshot_download
        snapshot_download(args.model, revision=args.revision)
        shard_paths = resolve_shard_paths(args.model, args.revision)
        total_mb = sum(os.path.getsize(p) for p in shard_paths) / (1024 ** 2)
        print(f"  Downloaded {len(shard_paths)} shards, {total_mb:.0f} MB total")

    # --- Run experiments ---
    all_results = []

    for exp_name in args.experiments:
        print(f"\n{'='*60}")
        print(f"Experiment: {exp_name}")
        print(f"{'='*60}")

        for rep in range(args.repeats):
            if args.repeats > 1:
                print(f"\n--- Repeat {rep + 1}/{args.repeats} ---")

            for _, config in EXPERIMENT_CONFIGS[exp_name]:
                # Drop caches before every single config run
                drop_caches()

                gc.collect()
                torch.cuda.empty_cache()
                torch.cuda.synchronize()

                try:
                    result = run_single_config(exp_name, config, args.model, gpu_ids,
                                               dtype, args.revision)
                    result.config["repeat"] = rep
                    print_result(result)
                    all_results.append(result)
                except Exception as e:
                    print(f"\n  [ERROR] {exp_name} config={config} failed: {e}")
                    import traceback
                    traceback.print_exc()
                    all_results.append(TimingResult(
                        experiment=exp_name, config={**config, "repeat": rep},
                        wall_time_s=0, error=str(e),
                    ))

                gc.collect()
                torch.cuda.empty_cache()
                torch.cuda.synchronize()

    # --- Cleanup junk file in background ---
    if junk_file:
        delete_junk_file_background(junk_file)

    # --- Summary ---
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"{'Experiment':<20} {'Config':<30} {'Wall Time (s)':<15} {'GPU Mem (MB)':<15}")
    print("-" * 80)
    for r in all_results:
        if not r.error:
            config_str = json.dumps(r.config, default=str)
            if len(config_str) > 28:
                config_str = config_str[:25] + "..."
            print(f"{r.experiment:<20} {config_str:<30} {r.wall_time_s:<15.2f} {r.peak_gpu_mem_mb:<15.0f}")

    # --- Save JSON ---
    if args.output:
        out = []
        for r in all_results:
            out.append({
                "experiment": r.experiment,
                "config": r.config,
                "wall_time_s": r.wall_time_s,
                "phase_times_s": r.phase_times_s,
                "peak_gpu_mem_mb": r.peak_gpu_mem_mb,
                "peak_cpu_mem_mb": r.peak_cpu_mem_mb,
                "error": r.error,
            })
        with open(args.output, "w") as f:
            json.dump({
                "model": args.model,
                "gpus": gpu_ids,
                "dtype": args.dtype,
                "cache_mode": "sudo" if use_sudo else f"junk_file_{JUNK_FILE_SIZE_GB}GB",
                "hostname": os.environ.get("HOSTNAME", "unknown"),
                "results": out,
            }, f, indent=2)
        print(f"\nResults saved to {args.output}")


if __name__ == "__main__":
    main()
