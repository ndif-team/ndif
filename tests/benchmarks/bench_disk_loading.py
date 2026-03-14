"""Benchmark: Model loading from disk with varying strategies.

Measures wall-clock time for loading HuggingFace models via from_pretrained
with different GLOBAL_WORKERS counts, run:ai model streamer, and vLLM to
compare loading strategies on Lustre / NVMe-oF storage (e.g., NCSA Delta).

Experiments:
  fio                 - raw sequential read bandwidth via fio (peak storage ceiling)
  baseline            - standard from_pretrained (v5 default, 4 worker threads)
  workers             - from_pretrained with GLOBAL_WORKERS = 1,2,4,8,16,32
  runai               - run:ai SafetensorsStreamer to CPU + from_pretrained(state_dict=...)
  nnsight             - nnsight's stream_weights_into_model (run:ai backend)
  vllm_default        - vLLM LLM() with default load_format="auto"
  vllm_runai          - vLLM LLM() with load_format="runai_streamer"

Environment notes:
  vLLM 0.17 pins transformers<5, but HF benchmarks need transformers v5.
  Use two conda envs:
    bench-hf   -> fio, baseline, workers, runai  (transformers 5.x)
    bench-vllm -> vllm_default, vllm_runai       (transformers 4.x, vllm 0.17)
  Missing-dep experiments are automatically skipped with a warning.

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

  # fio-only benchmark (no GPU needed)
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --experiments fio

  # Specific experiments only
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --experiments baseline workers

  # run:ai streamer benchmark (use bench-hf env)
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --experiments runai

  # vLLM benchmark (use bench-vllm env)
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --experiments vllm_default vllm_runai

  # Repeat each experiment N times
  python bench_disk_loading.py --model meta-llama/Llama-3.1-8B --repeats 3
"""

import argparse
import gc
import json
import os
import shutil
import subprocess
import sys
import time

# Force unbuffered stdout so progress is visible immediately
sys.stdout.reconfigure(line_buffering=True)
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import torch

# ---------------------------------------------------------------------------
# Dependency guards
# ---------------------------------------------------------------------------

def _has_runai_streamer() -> bool:
    try:
        from runai_model_streamer import SafetensorsStreamer  # noqa: F401
        return True
    except ImportError:
        return False

def _has_nnsight_loader() -> bool:
    try:
        from nnsight.modeling.loader import stream_to_state_dict  # noqa: F401
        return _has_runai_streamer()
    except ImportError:
        return False

def _has_vllm() -> bool:
    try:
        import vllm  # noqa: F401
        return True
    except ImportError:
        return False

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


def create_junk_file(path: str, size_gb: int, fio_binary: str):
    """Create a large junk file on local /tmp for page cache invalidation.

    Uses fio sequential write for speed. On Delta, /tmp is 1.5TB local NVMe
    so 512GB is safe. The content doesn't matter — we just need to fill the
    page cache with junk later.
    """
    if os.path.exists(path):
        existing_gb = os.path.getsize(path) / (1024 ** 3)
        if existing_gb >= size_gb * 0.99:
            print(f"  [junk] Reusing existing {existing_gb:.0f} GB file at {path}")
            return
        print(f"  [junk] Existing file too small ({existing_gb:.0f} GB), recreating...")

    print(f"  [junk] Creating {size_gb} GB junk file at {path} via fio...")
    t0 = time.perf_counter()
    subprocess.run(
        [fio_binary, "--name=create_junk", f"--filename={path}",
         "--rw=write", "--bs=1M", f"--size={size_gb}G",
         "--ioengine=psync", "--direct=0",
         "--eta=always", "--status-interval=5"],
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


def invalidate_via_junk_file(path: str, fio_binary: str, num_threads: int = 16):
    """Read the junk file to pressure model pages out of the page cache.

    Uses fio with buffered reads (direct=0) so data flows through the page
    cache, evicting model shard pages. Multiple jobs saturate I/O bandwidth.
    """
    if not os.path.exists(path):
        print(f"  [cache] Junk file not found: {path}")
        return

    file_size = os.path.getsize(path)
    size_gb = file_size / (1024 ** 3)
    chunk_gb = size_gb / num_threads
    print(f"  [cache] Reading {size_gb:.0f} GB junk file to evict page cache "
          f"(fio, {num_threads} jobs x {chunk_gb:.0f} GB)...")

    t0 = time.perf_counter()
    subprocess.run(
        [fio_binary, "--name=evict_cache", f"--filename={path}",
         "--rw=read", "--bs=1M", "--direct=0",
         f"--numjobs={num_threads}", f"--size={int(chunk_gb)}G",
         f"--offset_increment={int(chunk_gb)}G",
         "--ioengine=psync",
         "--group_reporting", "--thread",
         "--eta=always", "--status-interval=5"],
        check=True,
        timeout=600,
    )
    elapsed = time.perf_counter() - t0
    bw = size_gb / elapsed if elapsed > 0 else 0
    print(f"  [cache] Done in {elapsed:.1f}s ({bw:.1f} GB/s)")


# ---------------------------------------------------------------------------
# fio helpers
# ---------------------------------------------------------------------------

FIO_BUILD_DIR = "/tmp/fio-build"


def ensure_fio_binary() -> str:
    """Return path to an fio binary, building from source if needed."""
    system_fio = shutil.which("fio")
    if system_fio:
        print(f"  [fio] Found system fio: {system_fio}")
        return system_fio

    cached_fio = os.path.join(FIO_BUILD_DIR, "fio")
    if os.path.isfile(cached_fio) and os.access(cached_fio, os.X_OK):
        print(f"  [fio] Using cached build: {cached_fio}")
        return cached_fio

    print(f"  [fio] fio not found. Building from source in {FIO_BUILD_DIR}...")
    if os.path.exists(FIO_BUILD_DIR):
        shutil.rmtree(FIO_BUILD_DIR)

    subprocess.run(
        ["git", "clone", "--depth", "1", "git://git.kernel.dk/fio.git", FIO_BUILD_DIR],
        check=True,
    )
    subprocess.run(["./configure"], cwd=FIO_BUILD_DIR, check=True)
    nproc = os.cpu_count() or 4
    subprocess.run(["make", f"-j{nproc}"], cwd=FIO_BUILD_DIR, check=True)

    if not os.path.isfile(cached_fio):
        raise RuntimeError(f"fio build succeeded but binary not found at {cached_fio}")
    print(f"  [fio] Built successfully: {cached_fio}")
    return cached_fio


def check_libaio_available(fio_binary: str) -> bool:
    """Check if fio was built with libaio support."""
    try:
        result = subprocess.run(
            [fio_binary, "--enghelp"],
            capture_output=True, text=True, timeout=10,
        )
        return "libaio" in result.stdout
    except Exception:
        return False


def run_fio(shard_paths: list[str], numjobs: int, fio_binary: str,
            ioengine: str) -> TimingResult:
    """Run fio sequential read on the model shard files."""
    filename_arg = ":".join(shard_paths)
    cmd = [
        fio_binary,
        "--name=shard_read",
        f"--filename={filename_arg}",
        "--rw=read",
        "--direct=1",
        "--bs=1M",
        f"--ioengine={ioengine}",
        "--iodepth=64",
        f"--numjobs={numjobs}",
        "--readonly",
        "--group_reporting",
        "--output-format=json",
    ]

    print(f"  [fio] Running: numjobs={numjobs}, ioengine={ioengine}, {len(shard_paths)} shards")

    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    wall = time.perf_counter() - t0

    if result.returncode != 0:
        return TimingResult(
            experiment="fio",
            config={"numjobs": numjobs, "ioengine": ioengine},
            wall_time_s=wall,
            error=f"fio exited {result.returncode}: {result.stderr[:500]}",
        )

    fio_out = json.loads(result.stdout)
    job = fio_out["jobs"][0]["read"]
    bw_kbs = job["bw"]  # KB/s
    io_bytes = job["io_bytes"]
    runtime_ms = job["runtime"]

    bw_gbs = bw_kbs / (1024 * 1024)  # KB/s -> GB/s

    return TimingResult(
        experiment="fio",
        config={"numjobs": numjobs, "ioengine": ioengine},
        wall_time_s=runtime_ms / 1000.0,
        phase_times_s={
            "bw_gbs": bw_gbs,
            "io_bytes": io_bytes,
            "runtime_ms": runtime_ms,
        },
    )


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
            mem = torch.cuda.get_device_properties(i).total_memory
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
    print("  [baseline] Starting from_pretrained...", flush=True)
    t0 = time.perf_counter()

    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        revision=revision,
        device_map="auto",
        max_memory=max_memory,
        dtype=dtype,
        attn_implementation="eager",
        local_files_only=True,
    )

    print(f"  [baseline] from_pretrained returned, syncing CUDA...", flush=True)
    torch.cuda.synchronize()
    wall = time.perf_counter() - t0
    print(f"  [baseline] Done in {wall:.2f}s", flush=True)

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
            dtype=dtype,
            attn_implementation="eager",
            local_files_only=True,
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
# Experiment: run:ai SafetensorsStreamer
# ---------------------------------------------------------------------------

def run_runai_statedict(model_id: str, gpu_ids: list[int], dtype: torch.dtype,
                        shard_paths: list[str], concurrency: int = 16,
                        revision: str = "main") -> TimingResult:
    """Stream shards with pipelined pinned-memory GPU transfers (one tensor at a time)."""
    from runai_model_streamer import SafetensorsStreamer
    from transformers import AutoConfig, AutoModelForCausalLM
    from accelerate import init_empty_weights, infer_auto_device_map
    from accelerate.utils import set_module_tensor_to_device

    os.environ["RUNAI_STREAMER_CONCURRENCY"] = str(concurrency)
    max_memory = build_max_memory(gpu_ids)

    # 1. Init empty model on meta device, compute device map
    config = AutoConfig.from_pretrained(model_id, revision=revision, local_files_only=True)
    with init_empty_weights():
        model = AutoModelForCausalLM.from_config(
            config, dtype=dtype, attn_implementation="eager",
        )
    device_map = infer_auto_device_map(model, max_memory=max_memory, dtype=dtype)

    # 2. Expand module-level device_map to param-level lookup
    param_device = {}
    for name, _ in list(model.named_parameters()) + list(model.named_buffers()):
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in device_map:
                param_device[name] = device_map[prefix]
                break
        else:
            param_device[name] = device_map.get("", "cpu")

    # 3. Stream from disk -> dtype convert -> place on GPU
    torch.cuda.synchronize()
    t0 = time.perf_counter()

    with SafetensorsStreamer() as streamer:
        streamer.stream_files(shard_paths, device="cpu")
        for name, tensor in streamer.get_tensors():
            target = param_device.get(name, "cpu")
            if target not in ("cpu", "disk"):
                set_module_tensor_to_device(
                    model, name, target, value=tensor.to(dtype=dtype).to(target),
                    clear_cache=False,
                )
            else:
                set_module_tensor_to_device(
                    model, name, target, value=tensor.to(dtype=dtype),
                    clear_cache=False,
                )
            del tensor

    torch.cuda.synchronize()
    wall = time.perf_counter() - t0

    model.tie_weights()

    peak_gpu = get_gpu_mem_allocated_mb(gpu_ids)
    peak_cpu = get_process_rss_mb()
    unload_model(model)

    return TimingResult(
        experiment="runai",
        config={"concurrency": concurrency},
        wall_time_s=wall,
        peak_gpu_mem_mb=peak_gpu,
        peak_cpu_mem_mb=peak_cpu,
    )


# ---------------------------------------------------------------------------
# Experiment: nnsight loader (run:ai streamer via nnsight.modeling.loader)
# ---------------------------------------------------------------------------

def run_nnsight_loader(model_id: str, gpu_ids: list[int], dtype: torch.dtype,
                       shard_paths: list[str], concurrency: int = 16,
                       revision: str = "main") -> TimingResult:
    """Load model using nnsight's stream_to_state_dict (run:ai backend) + from_pretrained."""
    from nnsight.modeling.loader import stream_to_state_dict
    from transformers import AutoConfig, AutoModelForCausalLM

    max_memory = build_max_memory(gpu_ids)
    config = AutoConfig.from_pretrained(model_id, revision=revision, local_files_only=True)
    model_class = AutoModelForCausalLM._model_mapping[type(config)]

    torch.cuda.synchronize()
    t0 = time.perf_counter()

    state_dict = stream_to_state_dict(shard_paths, concurrency=concurrency)
    model = model_class.from_pretrained(
        None,
        config=config,
        state_dict=state_dict,
        device_map="auto",
        max_memory=max_memory,
        dtype=dtype,
        revision=revision,
    )

    torch.cuda.synchronize()
    wall = time.perf_counter() - t0

    peak_gpu = get_gpu_mem_allocated_mb(gpu_ids)
    peak_cpu = get_process_rss_mb()
    unload_model(model)

    return TimingResult(
        experiment="nnsight",
        config={"concurrency": concurrency},
        wall_time_s=wall,
        peak_gpu_mem_mb=peak_gpu,
        peak_cpu_mem_mb=peak_cpu,
    )


# ---------------------------------------------------------------------------
# Experiment: vLLM
# ---------------------------------------------------------------------------

def run_vllm(model_id: str, gpu_ids: list[int], dtype: torch.dtype,
             load_format: str = "auto", runai_concurrency: int = 16,
             gpu_memory_utilization: float = 0.9,
             revision: str = "main") -> TimingResult:
    """Load model via vLLM LLM() constructor. Measures time to inference readiness."""
    from vllm import LLM

    if load_format == "runai_streamer":
        os.environ["RUNAI_STREAMER_CONCURRENCY"] = str(runai_concurrency)

    # vLLM manages its own device placement via CUDA_VISIBLE_DEVICES
    orig_cvd = os.environ.get("CUDA_VISIBLE_DEVICES")
    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(str(g) for g in gpu_ids)

    config = {"load_format": load_format}
    if load_format == "runai_streamer":
        config["concurrency"] = runai_concurrency
    exp_name = "vllm_runai" if load_format == "runai_streamer" else "vllm_default"

    try:
        torch.cuda.synchronize()
        t0 = time.perf_counter()

        model = LLM(
            model=model_id,
            revision=revision,
            dtype="bfloat16",
            tensor_parallel_size=len(gpu_ids),
            load_format=load_format,
            gpu_memory_utilization=gpu_memory_utilization,
            enforce_eager=True,
        )

        torch.cuda.synchronize()
        wall = time.perf_counter() - t0

        peak_gpu = get_gpu_mem_allocated_mb(gpu_ids)
        peak_cpu = get_process_rss_mb()

        del model
        gc.collect()
        torch.cuda.empty_cache()

        return TimingResult(
            experiment=exp_name,
            config=config,
            wall_time_s=wall,
            peak_gpu_mem_mb=peak_gpu,
            peak_cpu_mem_mb=peak_cpu,
        )
    finally:
        # Restore CUDA_VISIBLE_DEVICES
        if orig_cvd is None:
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            os.environ["CUDA_VISIBLE_DEVICES"] = orig_cvd


# ---------------------------------------------------------------------------
# Experiment registry & dispatch
# ---------------------------------------------------------------------------

EXPERIMENT_CONFIGS = {
    "fio": [("fio", {"numjobs": n}) for n in [1, 4, 8, 16]],
    "baseline": [("baseline", {"workers": 4})],
    "workers": [("workers", {"workers": n}) for n in [1, 2, 4, 8, 16, 32]],
    "runai": [("runai", {"concurrency": c}) for c in [1, 4, 8, 16]],
    "nnsight": [("nnsight", {"concurrency": c}) for c in [1, 4, 8, 16]],
    "vllm_default": [("vllm_default", {"load_format": "auto"})],
    "vllm_runai": [("vllm_runai", {"load_format": "runai_streamer", "concurrency": c})
                   for c in [4, 8, 16, 32]],
}


def run_single_config(exp_name: str, config: dict, model_id: str, gpu_ids: list[int],
                      dtype: torch.dtype, revision: str = "main",
                      **kwargs) -> TimingResult:
    if exp_name == "fio":
        return run_fio(
            kwargs["shard_paths"], config["numjobs"],
            kwargs["fio_binary"], kwargs.get("ioengine", "libaio"),
        )
    elif exp_name == "baseline":
        return run_baseline(model_id, gpu_ids, dtype, revision)
    elif exp_name == "workers":
        return run_workers(model_id, gpu_ids, dtype, config["workers"], revision)
    elif exp_name == "runai":
        return run_runai_statedict(
            model_id, gpu_ids, dtype, kwargs["shard_paths"],
            concurrency=config["concurrency"], revision=revision,
        )
    elif exp_name == "nnsight":
        return run_nnsight_loader(
            model_id, gpu_ids, dtype, kwargs["shard_paths"],
            concurrency=config["concurrency"], revision=revision,
        )
    elif exp_name in ("vllm_default", "vllm_runai"):
        return run_vllm(
            model_id, gpu_ids, dtype,
            load_format=config["load_format"],
            runai_concurrency=config.get("concurrency", 16),
            gpu_memory_utilization=kwargs.get("gpu_memory_utilization", 0.9),
            revision=revision,
        )
    else:
        raise ValueError(f"Unknown experiment: {exp_name}")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_result(r: TimingResult):
    status = "ERROR" if r.error else "OK"
    print(f"\n  [{status}] {r.experiment} | config={r.config}", flush=True)
    if r.error:
        print(f"    error: {r.error}")
        return
    print(f"    wall_time:    {r.wall_time_s:.2f}s")
    if r.phase_times_s:
        for phase, val in r.phase_times_s.items():
            if isinstance(val, float):
                print(f"    {phase}: {val:.2f}")
            else:
                print(f"    {phase}: {val}")
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
    parser.add_argument("--no-drop-caches", action="store_true",
                        help="Skip all cache invalidation (warm-cache runs, faster testing)")
    parser.add_argument("--gpu-mem-util", type=float, default=0.9,
                        help="vLLM gpu_memory_utilization (default: 0.9, lower if GPUs are shared)")
    parser.add_argument("--output", default=None, help="JSON output file path")

    args = parser.parse_args()

    dtype_map = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}
    dtype = dtype_map[args.dtype]

    if args.gpus:
        gpu_ids = [int(x) for x in args.gpus.split(",")]
    else:
        gpu_ids = list(range(torch.cuda.device_count()))

    # --- Validate experiment dependencies ---
    EXPERIMENT_DEPS = {
        "runai": ("runai-model-streamer", _has_runai_streamer),
        "nnsight": ("nnsight + runai-model-streamer", _has_nnsight_loader),
        "vllm_default": ("vllm", _has_vllm),
        "vllm_runai": ("vllm + runai-model-streamer", lambda: _has_vllm() and _has_runai_streamer()),
    }
    valid_experiments = []
    for exp in args.experiments:
        if exp in EXPERIMENT_DEPS:
            pkg, check = EXPERIMENT_DEPS[exp]
            if not check():
                print(f"WARNING: '{exp}' requires {pkg}. Skipping.")
                continue
        valid_experiments.append(exp)
    args.experiments = valid_experiments

    if not args.experiments:
        print("ERROR: No valid experiments to run. Exiting.")
        sys.exit(1)

    print(f"Model:       {args.model}")
    print(f"GPUs:        {gpu_ids}")
    print(f"Dtype:       {args.dtype}")
    print(f"Repeats:     {args.repeats}")
    print(f"Experiments: {args.experiments}")

    # --- Cache invalidation setup ---
    use_sudo = args.sudo_drop_caches
    junk_file = None
    cache_fio_binary = None

    if use_sudo:
        print("Cache mode:  sudo drop_caches")
        # Verify sudo works
        try:
            subprocess.run(["sudo", "-n", "true"], check=True, timeout=5,
                           capture_output=True)
        except Exception:
            print("ERROR: --sudo-drop-caches requires passwordless sudo. Exiting.")
            sys.exit(1)
    elif args.no_drop_caches:
        print("Cache mode:  disabled (--no-drop-caches)")
    else:
        print(f"Cache mode:  junk file ({JUNK_FILE_SIZE_GB} GB on /tmp)")
        cache_fio_binary = ensure_fio_binary()
        junk_file = JUNK_FILE_PATH
        create_junk_file(junk_file, JUNK_FILE_SIZE_GB, cache_fio_binary)

    def drop_caches():
        if args.no_drop_caches:
            return
        if use_sudo:
            sudo_drop_caches()
        else:
            invalidate_via_junk_file(junk_file, cache_fio_binary)

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

    # --- fio setup ---
    fio_binary = cache_fio_binary  # reuse if already resolved for cache invalidation
    ioengine = "libaio"
    if "fio" in args.experiments:
        if fio_binary is None:
            fio_binary = ensure_fio_binary()
        if not check_libaio_available(fio_binary):
            print("  [fio] libaio not available, falling back to psync")
            ioengine = "psync"
        else:
            print(f"  [fio] Using ioengine: libaio")

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
                    print(f"  [run] Starting {exp_name} config={config}", flush=True)
                    result = run_single_config(exp_name, config, args.model, gpu_ids,
                                               dtype, args.revision,
                                               shard_paths=shard_paths,
                                               fio_binary=fio_binary,
                                               ioengine=ioengine,
                                               gpu_memory_utilization=args.gpu_mem_util)
                    print(f"  [run] Finished {exp_name}, printing result...", flush=True)
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

                print(f"  [run] Cleanup: gc + cuda empty_cache...", flush=True)
                gc.collect()
                torch.cuda.empty_cache()
                torch.cuda.synchronize()
                print(f"  [run] Cleanup done.", flush=True)

    # --- Cleanup junk file in background ---
    if junk_file:
        delete_junk_file_background(junk_file)

    # --- Compute effective BW for model-loading experiments ---
    total_gb = total_mb / 1024
    for r in all_results:
        if "bw_gbs" not in r.phase_times_s and r.wall_time_s > 0 and not r.error:
            r.phase_times_s["bw_gbs"] = total_gb / r.wall_time_s

    # --- Summary ---
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    print(f"{'Experiment':<20} {'Config':<30} {'Wall Time (s)':<15} {'BW (GB/s)':<12} {'GPU Mem (MB)':<15}")
    print("-" * 92)
    for r in all_results:
        if not r.error:
            config_str = json.dumps(r.config, default=str)
            if len(config_str) > 28:
                config_str = config_str[:25] + "..."
            bw_str = f"{r.phase_times_s['bw_gbs']:.2f}" if "bw_gbs" in r.phase_times_s else "N/A"
            print(f"{r.experiment:<20} {config_str:<30} {r.wall_time_s:<15.2f} {bw_str:<12} {r.peak_gpu_mem_mb:<15.0f}")

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
