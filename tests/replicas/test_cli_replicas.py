"""CLI integration tests for replica-aware scaling."""

import json
import os
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_REPO_ID = "openai-community/gpt2"
MODEL_REPO_ID_ALT = "openai-community/distilgpt2"
DEFAULT_TIMEOUT_S = 120
POLL_INTERVAL_S = 2


def _cli_env() -> dict[str, str]:
    env = os.environ.copy()
    ray_address = env.get("NDIF_RAY_ADDRESS")
    if not ray_address:
        pytest.skip("NDIF_RAY_ADDRESS must be set for CLI replica tests.")
    return env


def _run_cli(args: list[str]) -> str:
    result = __import__("subprocess").run(
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


def _status_json() -> dict:
    output = _run_cli(["status", "--json-output"])
    return json.loads(output)


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


def _hot_replica_count(status: dict, repo_id: str) -> int:
    return len(_deployment_replica_ids(status, repo_id))


def _wait_for_model_hot(repo_id: str, min_count: int, timeout_s: int) -> int:
    deadline = time.time() + timeout_s
    last_count = 0
    while time.time() < deadline:
        status = _status_json()
        last_count = _hot_replica_count(status, repo_id)
        if last_count >= min_count:
            return last_count
        time.sleep(POLL_INTERVAL_S)
    return last_count


def _wait_for_replica_count(repo_id: str, target: int, timeout_s: int) -> set[int]:
    deadline = time.time() + timeout_s
    last_replica_ids: set[int] = set()
    while time.time() < deadline:
        status = _status_json()
        last_replica_ids = _deployment_replica_ids(status, repo_id)
        if len(last_replica_ids) >= target:
            return last_replica_ids
        time.sleep(POLL_INTERVAL_S)
    return last_replica_ids


class TestReplicaCLI:
    """Validate replica scaling via CLI commands."""

    def test_scale_adds_replicas(self):
        desired_add = int(os.environ.get("NDIF_TEST_REPLICA_ADD", "2"))
        if desired_add <= 0:
            pytest.skip("NDIF_TEST_REPLICA_ADD must be positive to run this test.")

        current_replica_ids = _deployment_replica_ids(_status_json(), MODEL_REPO_ID)
        current_count = len(current_replica_ids)
        if current_count >= 8:
            pytest.skip("All GPUs already occupied by replicas; cannot scale further.")

        target = min(8, current_count + desired_add)
        _run_cli(["scale", MODEL_REPO_ID, "--replicas", str(target)])

        replica_ids = _wait_for_replica_count(
            MODEL_REPO_ID, target, DEFAULT_TIMEOUT_S
        )
        assert len(replica_ids) >= target, (
            f"Expected {target} replicas for {MODEL_REPO_ID}, "
            f"found {len(replica_ids)} ({sorted(replica_ids)})"
        )

    def test_deploy_second_model_replaces_replica(self):
        current_count = _hot_replica_count(_status_json(), MODEL_REPO_ID)
        if current_count < 8:
            _run_cli(["scale", MODEL_REPO_ID, "--replicas", "8"])
            current_count = _wait_for_model_hot(
                MODEL_REPO_ID, 8, DEFAULT_TIMEOUT_S
            )

        if current_count < 8:
            pytest.skip("Unable to reach full capacity (8 replicas); skipping eviction test.")

        _run_cli(["deploy", MODEL_REPO_ID_ALT, "--replicas", "1"])
        alt_count = _wait_for_model_hot(MODEL_REPO_ID_ALT, 1, DEFAULT_TIMEOUT_S)
        assert alt_count >= 1, "Second model failed to deploy."

        updated_count = _hot_replica_count(_status_json(), MODEL_REPO_ID)
        assert updated_count <= 7, (
            f"Expected {MODEL_REPO_ID} replicas to reduce after replacement; "
            f"found {updated_count}"
        )
