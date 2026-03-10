#!/usr/bin/env bash
# =============================================================================
# run_benchmark.sh — Full disk-loading benchmark for NCSA Delta
#
# Sets up two conda environments (bench-hf, bench-vllm) and runs all
# experiments: fio, baseline, workers, runai, vllm_default, vllm_runai.
#
# Prerequisites:
#   - conda available in PATH
#   - CUDA 12.x GPUs visible (nvidia-smi)
#   - Lustre or NVMe-oF storage at $HF_HOME (default ~/.cache/huggingface)
#   - For best results: sudo access (--sudo-drop-caches) or 512GB free on /tmp
#
# Usage:
#   bash run_benchmark.sh                         # all experiments, default model
#   bash run_benchmark.sh --model Qwen/Qwen2-7B-Instruct
#   bash run_benchmark.sh --gpus 0,1 --repeats 3
#   bash run_benchmark.sh --sudo                  # use sudo drop_caches
# =============================================================================
set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
MODEL="Qwen/Qwen2.5-7B-Instruct"
GPUS=""            # empty = all visible GPUs
REPEATS=1
SUDO_DROP_CACHES=false
NO_DROP_CACHES=false
OUTPUT_DIR="./benchmark_results"
SKIP_ENV_SETUP=false

# ── Parse arguments ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model)       MODEL="$2";           shift 2 ;;
    --gpus)        GPUS="$2";            shift 2 ;;
    --repeats)     REPEATS="$2";         shift 2 ;;
    --sudo)        SUDO_DROP_CACHES=true; shift ;;
    --no-drop-caches) NO_DROP_CACHES=true; shift ;;
    --output-dir)  OUTPUT_DIR="$2";      shift 2 ;;
    --skip-env-setup) SKIP_ENV_SETUP=true; shift ;;
    -h|--help)
      sed -n '2,/^# =====/p' "$0" | head -n -1 | sed 's/^# \?//'
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BENCH_SCRIPT="$SCRIPT_DIR/bench_disk_loading.py"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
HOSTNAME_SHORT="$(hostname -s)"

mkdir -p "$OUTPUT_DIR"

echo "============================================================"
echo "Disk Loading Benchmark"
echo "============================================================"
echo "Model:      $MODEL"
echo "GPUs:       ${GPUS:-all visible}"
echo "Repeats:    $REPEATS"
echo "Output dir: $OUTPUT_DIR"
echo "Timestamp:  $TIMESTAMP"
echo "Host:       $HOSTNAME_SHORT"
echo ""

# ── Helper: conda env exists? ────────────────────────────────────────────────
env_exists() {
  conda info --envs 2>/dev/null | grep -qw "$1"
}

# =============================================================================
# 1. Environment setup
# =============================================================================
if [[ "$SKIP_ENV_SETUP" == "false" ]]; then
  echo "──── Setting up conda environments ────"

  # ── bench-hf: transformers 5.x + runai streamer ──────────────────────────
  if ! env_exists bench-hf; then
    echo "[bench-hf] Creating environment..."
    conda create -y -n bench-hf python=3.12
  else
    echo "[bench-hf] Environment already exists."
  fi

  echo "[bench-hf] Installing/updating packages..."
  conda run -n bench-hf --no-banner pip install -q \
    torch \
    "transformers>=5.0" \
    accelerate \
    safetensors \
    huggingface_hub \
    runai-model-streamer \
    runai-model-streamer-s3   # S3 support (optional, harmless if unused)

  # ── bench-vllm: vLLM 0.17 (pins transformers<5) ─────────────────────────
  if ! env_exists bench-vllm; then
    echo "[bench-vllm] Creating environment..."
    conda create -y -n bench-vllm python=3.12
  else
    echo "[bench-vllm] Environment already exists."
  fi

  echo "[bench-vllm] Installing/updating packages..."
  conda run -n bench-vllm --no-banner pip install -q \
    "vllm>=0.15" \
    runai-model-streamer \
    runai-model-streamer-s3

  echo ""
else
  echo "──── Skipping env setup (--skip-env-setup) ────"
  echo ""
fi

# ── Resolve python paths ────────────────────────────────────────────────────
PYTHON_HF="$(conda run -n bench-hf --no-banner which python)"
PYTHON_VLLM="$(conda run -n bench-vllm --no-banner which python)"

echo "bench-hf python:   $PYTHON_HF"
echo "bench-vllm python: $PYTHON_VLLM"
echo ""

# ── Build common args ───────────────────────────────────────────────────────
COMMON_ARGS=(--model "$MODEL" --repeats "$REPEATS")
[[ -n "$GPUS" ]] && COMMON_ARGS+=(--gpus "$GPUS")
if [[ "$SUDO_DROP_CACHES" == "true" ]]; then
  COMMON_ARGS+=(--sudo-drop-caches)
elif [[ "$NO_DROP_CACHES" == "true" ]]; then
  COMMON_ARGS+=(--no-drop-caches)
fi

# =============================================================================
# 2. Run HF experiments (fio, baseline, workers, runai)
# =============================================================================
HF_OUT="$OUTPUT_DIR/${TIMESTAMP}_${HOSTNAME_SHORT}_hf.json"

echo "============================================================"
echo "Phase 1: HF experiments (fio, baseline, workers, runai)"
echo "  env:    bench-hf"
echo "  output: $HF_OUT"
echo "============================================================"
echo ""

"$PYTHON_HF" "$BENCH_SCRIPT" \
  "${COMMON_ARGS[@]}" \
  --experiments fio baseline workers runai \
  --output "$HF_OUT"

echo ""

# =============================================================================
# 3. Run vLLM experiments (vllm_default, vllm_runai)
# =============================================================================
VLLM_OUT="$OUTPUT_DIR/${TIMESTAMP}_${HOSTNAME_SHORT}_vllm.json"

echo "============================================================"
echo "Phase 2: vLLM experiments (vllm_default, vllm_runai)"
echo "  env:    bench-vllm"
echo "  output: $VLLM_OUT"
echo "============================================================"
echo ""

"$PYTHON_VLLM" "$BENCH_SCRIPT" \
  "${COMMON_ARGS[@]}" \
  --experiments vllm_default vllm_runai \
  --output "$VLLM_OUT"

echo ""

# =============================================================================
# 4. Done
# =============================================================================
echo "============================================================"
echo "All benchmarks complete."
echo "  HF results:   $HF_OUT"
echo "  vLLM results: $VLLM_OUT"
echo "============================================================"
