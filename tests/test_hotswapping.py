"""
Integration tests for fractional GPU deployment and hotswapping.

Tests verify cluster resource management:
- Single-GPU fractional model deployments
- Multi-GPU model deployments
- HOT -> WARM eviction transitions
- WARM -> HOT cache redeploy transitions
- COLD -> HOT first-time deployment
- GPU resource accounting

Run with:
    conda activate ns312
    pytest tests/test_hotswapping.py --run-remote -v

Assumes:
- NDIF server running at http://localhost:5001
- 2x A100 80GB GPUs on the cluster (device_ids: 1,2)
- Models already downloaded on the cluster
"""

import time

import pytest
import requests
from nnsight import LanguageModel


# =============================================================================
# Helpers
# =============================================================================


def get_status(host):
    """Fetch cluster status from the NDIF API."""
    # Sleep briefly to let the 1s status cache expire
    time.sleep(2)
    resp = requests.get(f"{host}/status", timeout=120)
    resp.raise_for_status()
    return resp.json()


def find_deployment(status, repo_id):
    """Find a deployment by repo_id in the status response."""
    for _name, info in status.get("deployments", {}).items():
        if info.get("repo_id") == repo_id:
            return info
    return None


def find_gpu_allocation(status, repo_id):
    """Find the GPU allocation dict for a model from cluster node info.

    Returns dict mapping str(gpu_index) -> bytes allocated, or None.
    """
    for _node_id, node in status.get("cluster", {}).get("nodes", {}).items():
        for mk, dep_info in node.get("deployments", {}).items():
            if repo_id in mk:
                return dep_info.get("gpus", {})
    return None


def get_all_gpu_details(status):
    """Get all GPU details across all nodes."""
    details = []
    for _node_id, node in status.get("cluster", {}).get("nodes", {}).items():
        for gpu in node.get("resources", {}).get("gpu_details", []):
            details.append(gpu)
    return details


def run_trace(model, prompt="Hello world"):
    """Run a simple remote trace and return the output."""
    with model.trace(prompt, remote=True):
        output = model.output.save()
    return output


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def host(ndif_host):
    return ndif_host


@pytest.fixture(scope="module")
def qwen_0_5b():
    return LanguageModel("Qwen/Qwen2.5-0.5B")


@pytest.fixture(scope="module")
def qwen_1_5b():
    return LanguageModel("Qwen/Qwen2.5-1.5B")


@pytest.fixture(scope="module")
def qwen_3b():
    return LanguageModel("Qwen/Qwen2.5-3B")


@pytest.fixture(scope="module")
def qwen_7b():
    return LanguageModel("Qwen/Qwen2.5-7B")


@pytest.fixture(scope="module")
def llama_8b():
    return LanguageModel("meta-llama/Llama-3.1-8B")


@pytest.fixture(scope="module")
def llama_70b():
    return LanguageModel("meta-llama/Llama-3.1-70B")


# =============================================================================
# Single-GPU Fractional Deployment (COLD -> HOT)
# =============================================================================


class TestFractionalSingleGPU:
    """Test deploying small models that use a fraction of one GPU."""

    def test_deploy_small_model(self, host, qwen_0_5b):
        """COLD -> HOT: A 0.5B model should deploy on exactly 1 GPU with fractional memory."""
        output = run_trace(qwen_0_5b)
        assert output is not None

        status = get_status(host)
        info = find_deployment(status, "Qwen/Qwen2.5-0.5B")
        assert info is not None
        assert info["deployment_level"] == "HOT"

        gpus = find_gpu_allocation(status, "Qwen/Qwen2.5-0.5B")
        assert gpus is not None, "Model should have GPU allocation"
        assert len(gpus) == 1, f"0.5B model should use 1 GPU, got {len(gpus)}"

        bytes_used = list(gpus.values())[0]
        assert bytes_used < 10 * 1024**3, "0.5B model should use <10GB"

    def test_two_small_models_coexist(self, host, qwen_0_5b, qwen_1_5b):
        """Two small models should both be HOT simultaneously."""
        run_trace(qwen_0_5b)
        run_trace(qwen_1_5b)

        status = get_status(host)
        info_0_5b = find_deployment(status, "Qwen/Qwen2.5-0.5B")
        info_1_5b = find_deployment(status, "Qwen/Qwen2.5-1.5B")
        assert info_0_5b is not None and info_0_5b["deployment_level"] == "HOT"
        assert info_1_5b is not None and info_1_5b["deployment_level"] == "HOT"

    def test_fractional_models_share_gpu(self, host, qwen_0_5b, qwen_1_5b):
        """Small models should be bin-packed onto the same GPU (best-fit)."""
        run_trace(qwen_0_5b)
        run_trace(qwen_1_5b)

        status = get_status(host)
        gpus_0_5b = find_gpu_allocation(status, "Qwen/Qwen2.5-0.5B")
        gpus_1_5b = find_gpu_allocation(status, "Qwen/Qwen2.5-1.5B")

        gpu_idx_0_5b = set(gpus_0_5b.keys())
        gpu_idx_1_5b = set(gpus_1_5b.keys())

        assert gpu_idx_0_5b == gpu_idx_1_5b, (
            f"Expected both small models on the same GPU, "
            f"got {gpu_idx_0_5b} and {gpu_idx_1_5b}"
        )

    def test_medium_model_single_gpu(self, host, qwen_7b):
        """A 7B model (~14GB) should still fit on a single A100 80GB."""
        output = run_trace(qwen_7b)
        assert output is not None

        status = get_status(host)
        info = find_deployment(status, "Qwen/Qwen2.5-7B")
        assert info is not None
        assert info["deployment_level"] == "HOT"

        gpus = find_gpu_allocation(status, "Qwen/Qwen2.5-7B")
        assert len(gpus) == 1, f"7B model should use 1 GPU, got {len(gpus)}"

        bytes_used = list(gpus.values())[0]
        assert bytes_used < 40 * 1024**3, "7B model should use <40GB"
        assert bytes_used > 5 * 1024**3, "7B model should use >5GB"


# =============================================================================
# Multi-GPU Deployment
# =============================================================================


class TestMultiGPUDeployment:
    """Test deploying models that span multiple GPUs."""

    def test_deploy_70b_model(self, host, llama_70b):
        """COLD -> HOT: A 70B model should span multiple GPUs, each fully allocated."""
        output = run_trace(llama_70b, prompt="The capital of France is")
        assert output is not None

        status = get_status(host)
        info = find_deployment(status, "meta-llama/Llama-3.1-70B")
        assert info is not None
        assert info["deployment_level"] == "HOT"

        gpus = find_gpu_allocation(status, "meta-llama/Llama-3.1-70B")
        assert gpus is not None
        assert len(gpus) >= 2, f"70B model should use >=2 GPUs, got {len(gpus)}"

        # Each GPU in a multi-GPU deployment should use full GPU memory
        gpu_details = get_all_gpu_details(status)
        per_gpu_memory = gpu_details[0]["memory_bytes"]
        for gpu_idx, bytes_used in gpus.items():
            assert bytes_used == per_gpu_memory, (
                f"Multi-GPU model should consume full GPU memory on each GPU, "
                f"got {bytes_used / 1024**3:.1f}GB on GPU {gpu_idx}"
            )

    def test_70b_evicts_smaller_models(self, host, qwen_7b, llama_70b):
        """HOT -> WARM: Deploying 70B should evict smaller models from GPU to cache."""
        # Ensure 7B is deployed
        run_trace(qwen_7b)
        status = get_status(host)
        info_7b = find_deployment(status, "Qwen/Qwen2.5-7B")
        assert info_7b is not None and info_7b["deployment_level"] == "HOT"

        # Deploy 70B - with only 2 GPUs, this must evict the 7B
        run_trace(llama_70b, prompt="Hello")

        status = get_status(host)
        info_70b = find_deployment(status, "meta-llama/Llama-3.1-70B")
        assert info_70b is not None
        assert info_70b["deployment_level"] == "HOT"

        # 7B should be evicted to WARM (CPU cache)
        info_7b = find_deployment(status, "Qwen/Qwen2.5-7B")
        assert info_7b is not None, "Evicted model should still appear in status as WARM"
        assert info_7b["deployment_level"] == "WARM", (
            f"Expected 7B to be WARM after eviction, got {info_7b['deployment_level']}"
        )


# =============================================================================
# Eviction Transitions
# =============================================================================


class TestEvictionTransitions:
    """Test the full lifecycle of model state transitions."""

    def test_hot_to_warm_on_eviction(self, host, qwen_7b, llama_70b):
        """HOT -> WARM: An evicted model should move to CPU cache."""
        # Deploy 7B first
        run_trace(qwen_7b)
        status = get_status(host)
        assert find_deployment(status, "Qwen/Qwen2.5-7B")["deployment_level"] == "HOT"

        # Deploy 70B to force eviction of 7B (70B needs both GPUs)
        run_trace(llama_70b, prompt="Test")

        status = get_status(host)
        info_7b = find_deployment(status, "Qwen/Qwen2.5-7B")
        assert info_7b is not None
        assert info_7b["deployment_level"] == "WARM"

        # 7B should no longer have a GPU allocation
        gpus = find_gpu_allocation(status, "Qwen/Qwen2.5-7B")
        assert gpus is None, "WARM model should not have GPU allocation"

    def test_warm_to_hot_from_cache(self, host, qwen_7b, llama_70b):
        """WARM -> HOT: Redeploying a cached model should bring it back to GPU."""
        # Set up: deploy 7B, then evict it with 70B
        run_trace(qwen_7b)
        run_trace(llama_70b, prompt="Evict")

        status = get_status(host)
        info = find_deployment(status, "Qwen/Qwen2.5-7B")
        assert info is not None and info["deployment_level"] == "WARM"

        # Now redeploy 7B - should come from cache
        output = run_trace(qwen_7b, prompt="Back from cache")
        assert output is not None

        status = get_status(host)
        info = find_deployment(status, "Qwen/Qwen2.5-7B")
        assert info is not None
        assert info["deployment_level"] == "HOT"

        gpus = find_gpu_allocation(status, "Qwen/Qwen2.5-7B")
        assert gpus is not None and len(gpus) == 1

    def test_warm_model_survives_eviction_cycle(self, host, llama_8b, llama_70b):
        """A model evicted to WARM should still produce valid output when redeployed."""
        # Cold deploy a model not used in any previous test
        out_before = run_trace(llama_8b, prompt="Cold deploy")
        assert out_before is not None

        status = get_status(host)
        assert find_deployment(status, "meta-llama/Llama-3.1-8B")["deployment_level"] == "HOT"

        # Evict to WARM by deploying 70B (needs both GPUs)
        run_trace(llama_70b, prompt="Evict")
        status = get_status(host)
        assert find_deployment(status, "meta-llama/Llama-3.1-8B")["deployment_level"] == "WARM"

        # Redeploy from WARM cache - should work and produce valid output
        out_after = run_trace(llama_8b, prompt="From cache")
        assert out_after is not None

        status = get_status(host)
        assert find_deployment(status, "meta-llama/Llama-3.1-8B")["deployment_level"] == "HOT"


# =============================================================================
# Hotswapping
# =============================================================================


class TestHotswapping:
    """Test deploying, evicting, and redeploying models (hotswap cycle)."""

    def test_redeploy_after_eviction(self, host, qwen_3b, llama_70b):
        """A model should work correctly after being evicted and redeployed."""
        # Deploy 3B
        run_trace(qwen_3b)

        # Evict 3B by deploying 70B (needs both GPUs)
        run_trace(llama_70b, prompt="Evict")

        # Redeploy 3B from cache - should produce valid output
        output = run_trace(qwen_3b, prompt="Testing redeploy")
        assert output is not None

        status = get_status(host)
        info = find_deployment(status, "Qwen/Qwen2.5-3B")
        assert info is not None
        assert info["deployment_level"] == "HOT"

    def test_hotswap_produces_correct_output(self, host, qwen_0_5b, llama_70b):
        """Hotswapped models should produce valid outputs after redeploy."""
        # Deploy small model and get output
        out_before = run_trace(qwen_0_5b, prompt="2 + 2 =")
        assert out_before is not None

        # Evict it with 70B
        run_trace(llama_70b, prompt="2 + 2 =")

        # Redeploy from cache and get output again
        out_after = run_trace(qwen_0_5b, prompt="2 + 2 =")
        assert out_after is not None

    def test_rapid_model_switching(self, host, qwen_0_5b, qwen_1_5b, qwen_3b):
        """Rapidly switching between small models should not cause errors."""
        models = [
            (qwen_0_5b, "Qwen/Qwen2.5-0.5B"),
            (qwen_1_5b, "Qwen/Qwen2.5-1.5B"),
            (qwen_3b, "Qwen/Qwen2.5-3B"),
        ]
        for model, key in models:
            output = run_trace(model, prompt="Quick test")
            assert output is not None, f"Failed to get output from {key}"


# =============================================================================
# GPU Resource Accounting
# =============================================================================


class TestGPUResourceAccounting:
    """Test that GPU memory tracking is correct after operations."""

    def test_available_memory_valid(self, host, qwen_0_5b):
        """Available GPU memory should always be between 0 and total."""
        run_trace(qwen_0_5b)
        status = get_status(host)

        for gpu in get_all_gpu_details(status):
            total = gpu["memory_bytes"]
            available = gpu["available_memory_bytes"]
            assert 0 <= available <= total, (
                f"GPU {gpu['index']}: available={available} not in [0, {total}]"
            )

    def test_allocated_memory_matches_deployment(self, host, qwen_0_5b):
        """Sum of all deployment allocations should match GPU used memory."""
        run_trace(qwen_0_5b)
        status = get_status(host)

        for _node_id, node in status["cluster"]["nodes"].items():
            gpu_total_alloc = {}
            for _mk, dep in node["deployments"].items():
                for gpu_idx, bytes_used in dep["gpus"].items():
                    gpu_total_alloc[gpu_idx] = gpu_total_alloc.get(gpu_idx, 0) + bytes_used

            for gpu in node["resources"]["gpu_details"]:
                idx = str(gpu["index"])
                total = gpu["memory_bytes"]
                available = gpu["available_memory_bytes"]
                expected_used = gpu_total_alloc.get(idx, 0)
                actual_used = total - available

                assert abs(actual_used - expected_used) < 1, (
                    f"GPU {idx}: total={total}, available={available}, "
                    f"expected_used={expected_used}, actual_used={actual_used}"
                )

    def test_total_gpu_count(self, host):
        """Cluster should report 2 GPUs."""
        status = get_status(host)
        all_gpus = get_all_gpu_details(status)
        assert len(all_gpus) == 2, f"Expected 2 GPUs, got {len(all_gpus)}"

        for gpu in all_gpus:
            memory_gb = gpu["memory_bytes"] / 1024**3
            assert 75 < memory_gb < 85, f"Expected ~80GB GPU, got {memory_gb:.1f}GB"
