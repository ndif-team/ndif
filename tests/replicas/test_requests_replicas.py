"""Replica request tests using NNsight remote execution."""

import os
import time
from typing import Iterable

import pytest
import requests
from nnsight import LanguageModel
import sys
from pathlib import Path
import subprocess


MODEL_REPO_ID = "openai-community/gpt2"
DEFAULT_TIMEOUT_S = 120
POLL_INTERVAL_S = 2
REPO_ROOT = Path(__file__).resolve().parents[2]


def _expected_replica_count() -> int:
    raw = os.environ.get("NDIF_TEST_REPLICA_COUNT") or os.environ.get(
        "NDIF_MODEL_REPLICAS_DEFAULT", "1"
    )
    try:
        return int(raw)
    except ValueError:
        return 1


def _deployment_replica_ids(status: dict, repo_id: str) -> set[int]:
    deployments = status.get("deployments", {})
    replica_ids = set()
    for deployment in deployments.values():
        if deployment.get("repo_id") != repo_id:
            continue
        if deployment.get("deployment_level") != "HOT":
            continue
        replica_id = deployment.get("replica_id")
        if replica_id is not None:
            replica_ids.add(int(replica_id))
    return replica_ids


def _wait_for_replicas(
    ndif_host: str, repo_id: str, expected_count: int, timeout_s: int
) -> set[int]:
    deadline = time.time() + timeout_s
    last_replica_ids: set[int] = set()
    while time.time() < deadline:
        response = requests.get(f"{ndif_host}/status", timeout=10)
        response.raise_for_status()
        status = response.json()
        last_replica_ids = _deployment_replica_ids(status, repo_id)
        if len(last_replica_ids) >= expected_count:
            return last_replica_ids
        time.sleep(POLL_INTERVAL_S)
    return last_replica_ids


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    ray_address = env.get("NDIF_RAY_ADDRESS")
    if not ray_address:
        pytest.skip("NDIF_RAY_ADDRESS must be set for CLI replica tests.")
    return env


def _run_cli(args: list[str]) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "cli.cli", *args],
        cwd=REPO_ROOT,
        env=_cli_env(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"CLI failed: {' '.join(args)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


class TestReplicaRequests:
    """Validate replica-aware request handling."""

    def test_request_triggers_multiple_replicas(self, ndif_host: str):
        expected = _expected_replica_count()
        if expected > 8:
            pytest.skip("Expected replicas exceed available GPUs (8)")

        model = LanguageModel(MODEL_REPO_ID)

        with model.trace("The Eiffel Tower is located in ", remote=True):
            output = model.output.save()

        assert output is not None

        replica_ids = _wait_for_replicas(
            ndif_host, MODEL_REPO_ID, expected, DEFAULT_TIMEOUT_S
        )
        assert len(replica_ids) >= expected, (
            f"Expected at least {expected} replicas for {MODEL_REPO_ID}, "
            f"found {len(replica_ids)} ({sorted(replica_ids)})"
        )

    def test_evicted_replica_still_serves_requests(self, ndif_host: str):
        expected = max(2, _expected_replica_count())
        if expected > 8:
            pytest.skip("Expected replicas exceed available GPUs (8)")

        replica_ids = _wait_for_replicas(
            ndif_host, MODEL_REPO_ID, expected, DEFAULT_TIMEOUT_S
        )
        if len(replica_ids) < expected:
            _run_cli(["scale", MODEL_REPO_ID, "--replicas", str(expected)])
            replica_ids = _wait_for_replicas(
                ndif_host, MODEL_REPO_ID, expected, DEFAULT_TIMEOUT_S
            )

        if len(replica_ids) < 2:
            pytest.skip("Need at least 2 replicas to test eviction behavior.")

        evict_replica_id = sorted(replica_ids)[0]
        _run_cli(["evict", MODEL_REPO_ID, "--replica-id", str(evict_replica_id)])

        remaining_replica_ids = _wait_for_replicas(
            ndif_host, MODEL_REPO_ID, max(1, expected - 1), DEFAULT_TIMEOUT_S
        )
        if evict_replica_id in remaining_replica_ids:
            pytest.skip("Replica eviction did not apply yet; skipping request test.")

        model = LanguageModel(MODEL_REPO_ID)
        with model.trace("The Eiffel Tower is located in ", remote=True):
            output = model.output.save()

        assert output is not None
        assert len(remaining_replica_ids) >= 1

    def test_multiple_requests(self, ndif_host: str):
        expected = _expected_replica_count()
        if expected > 8:
            pytest.skip("Expected replicas exceed available GPUs (8)")

        _wait_for_replicas(ndif_host, MODEL_REPO_ID, max(1, expected), DEFAULT_TIMEOUT_S)

        prompts = [
            "The Eiffel Tower is located in ",
            "Madison Square Garden is located in the city of ",
            "The capital of France is ",
            "The tallest mountain in the world is ",
        ]

        def _run_prompt(prompt: str):
            model = LanguageModel(MODEL_REPO_ID)
            with model.trace(prompt, remote=True):
                return model.output.save()

        outputs = []
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, expected)) as pool:
            futures = [pool.submit(_run_prompt, prompt) for prompt in prompts]
            for future in futures:
                outputs.append(future.result())

        assert all(output is not None for output in outputs)
