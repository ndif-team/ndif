"""Shared helpers for the tests/report/ suite.

Helpers live here (not in conftest.py) so test modules can import them
normally — pytest's conftest mechanism is for fixtures, not for module-
importable functions.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from typing import Callable, List, Optional, Tuple

import pytest
import requests
from nnsight import LanguageModel


# ---------------------------------------------------------------------------
# Model repo ids (matches the fixtures in conftest.py)
# ---------------------------------------------------------------------------

GPT2_REPO = "openai-community/gpt2"
QWEN_05B_REPO = "Qwen/Qwen2.5-0.5B"
QWEN_15B_REPO = "Qwen/Qwen2.5-1.5B"
QWEN_7B_REPO = "Qwen/Qwen2.5-7B"
LLAMA_8B_REPO = "meta-llama/Llama-3.1-8B"
LLAMA_70B_REPO = "meta-llama/Llama-3.1-70B"


# ---------------------------------------------------------------------------
# /status helpers
# ---------------------------------------------------------------------------


def get_status(host: str) -> dict:
    """Fetch raw controller status from the NDIF API.

    Sleeps briefly so we never read a stale /status from the API's redis
    cache (TTL = ``NDIF_STATUS_CACHE_FREQ_S=10`` in the test stack).
    """
    time.sleep(2)
    resp = requests.get(f"{host}/status", timeout=120)
    resp.raise_for_status()
    return resp.json()


def find_replicas(status: dict, repo_id: str) -> List[dict]:
    """Per-replica entries in /status that belong to ``repo_id``.

    The raw controller /status emits one entry per replica (HOT and WARM
    both appear; COLD HF-cache shadows have no ``replica_id`` and are
    excluded).
    """
    return [
        info
        for _name, info in status.get("deployments", {}).items()
        if info.get("repo_id") == repo_id and info.get("replica_id")
    ]


def find_levels(status: dict, repo_id: str) -> List[str]:
    """Per-replica deployment_level list (HOT/WARM) for repo_id."""
    return [r.get("deployment_level") for r in find_replicas(status, repo_id)]


def find_any_level(status: dict, repo_id: str) -> Optional[str]:
    """Pick the "best" level for repo_id — HOT > WARM > None."""
    levels = set(find_levels(status, repo_id))
    if "HOT" in levels:
        return "HOT"
    if "WARM" in levels:
        return "WARM"
    return None


def find_replica_ids(status: dict, repo_id: str) -> List[str]:
    """Replica id strings for repo_id (HOT + WARM)."""
    return [r["replica_id"] for r in find_replicas(status, repo_id)]


def count_replicas(status: dict, repo_id: str, level: Optional[str] = None) -> int:
    """How many replicas of repo_id are at ``level`` (None = any)."""
    replicas = find_replicas(status, repo_id)
    if level is None:
        return len(replicas)
    return sum(1 for r in replicas if r.get("deployment_level") == level)


def wait_for_replica_count(
    host: str,
    repo_id: str,
    count: int,
    level: str = "HOT",
    timeout: int = 120,
) -> int:
    """Block until ``count`` replicas of ``repo_id`` are at ``level``.

    Returns the count actually observed (>= ``count`` on success, the last
    seen value on timeout).
    """
    end = time.time() + timeout
    last = 0
    while time.time() < end:
        last = count_replicas(get_status(host), repo_id, level=level)
        if last >= count:
            return last
        time.sleep(1)
    return last


def wait_for_no_replicas(
    host: str, repo_id: str, timeout: int = 60
) -> bool:
    """Block until ``repo_id`` has no HOT or WARM replicas."""
    end = time.time() + timeout
    while time.time() < end:
        if not find_replicas(get_status(host), repo_id):
            return True
        time.sleep(1)
    return False


# ---------------------------------------------------------------------------
# Controller-side helpers (deploy / evict via the CLI lib in-process)
# ---------------------------------------------------------------------------


def model_key_from_repo(repo_id: str, revision: Optional[str] = None) -> str:
    """Wrap cli.lib.util.get_model_key — gives the exact canonical key the
    controller stores deployments under."""
    from ndif.cli.lib.util import get_model_key

    return get_model_key(repo_id, revision)


def deploy_via_lib(
    repo_id: str,
    *,
    replicas: int = 1,
    pinned: bool = False,
    revision: Optional[str] = None,
) -> dict:
    """Call cli.lib.deploy.deploy in-process — same path the dashboard takes."""
    from ndif.cli.lib.deploy import deploy as _deploy

    spec = {
        "checkpoint": repo_id,
        "revision": revision,
        "pinned": pinned,
        "replicas": replicas,
    }
    return _deploy([spec])


def evict_via_lib(
    repo_id: str,
    *,
    replica: Optional[str] = None,
    revision: Optional[str] = None,
) -> dict:
    """Call cli.lib.evict.evict in-process. Targets one checkpoint.

    With ``replica`` set, targets one replica (HOT may demote to WARM).
    Without, fan-out evicts every HOT + WARM replica.
    """
    from ndif.cli.lib.evict import evict as _evict

    return _evict(
        checkpoints=[(repo_id, revision)],
        replica=replica,
    )


def evict_all_models() -> dict:
    """Evict every currently HOT model — convenience for test setup/teardown."""
    from ndif.cli.lib.evict import evict as _evict

    return _evict(evict_all=True)


# ---------------------------------------------------------------------------
# Response capture — record every status string the nnsight client sees
# ---------------------------------------------------------------------------


CapturedResponse = Tuple[str, str]  # (status_name, description)


@contextlib.contextmanager
def capture_responses():
    """Monkey-patch nnsight's RemoteBackend.handle_response so every
    response the client receives is recorded as ``(status_name,
    description)`` in the yielded list, in order.

    The canonical way to ask "what does the user see when X happens?"
    """
    from nnsight.intervention.backends import remote as _remote_module

    recorded: List[CapturedResponse] = []
    original = _remote_module.RemoteBackend.handle_response

    def patched(self, response, *args, **kwargs):
        try:
            recorded.append((response.status.name, response.description or ""))
        except Exception:
            recorded.append(("<unknown>", ""))
        return original(self, response, *args, **kwargs)

    _remote_module.RemoteBackend.handle_response = patched
    try:
        yield recorded
    finally:
        _remote_module.RemoteBackend.handle_response = original


# ---------------------------------------------------------------------------
# GPU memory gating
# ---------------------------------------------------------------------------


def free_gpu_bytes() -> List[int]:
    """Per-GPU free memory in bytes, as host-side nvidia-smi reports it."""
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=10,
        )
    except Exception:
        return []
    free_mib = [int(line.strip()) for line in out.splitlines() if line.strip()]
    return [mib * 1024 * 1024 for mib in free_mib]


def skip_if_insufficient_gpu(need_bytes: int, num_gpus: int = 1) -> None:
    """Skip if the host doesn't have ``num_gpus`` cards with ``need_bytes`` free."""
    free = free_gpu_bytes()
    qualifying = sum(1 for f in free if f >= need_bytes)
    if qualifying < num_gpus:
        pytest.skip(
            f"need {num_gpus} GPU(s) with ≥{need_bytes / 1024**3:.1f}GB free; "
            f"have {qualifying} ({[f'{f / 1024**3:.1f}' for f in free]} GB free)"
        )


# ---------------------------------------------------------------------------
# Trace helpers
# ---------------------------------------------------------------------------


def run_trace(model: LanguageModel, prompt: str = "Hello world"):
    """Run a simple remote trace and return the saved output."""
    with model.trace(prompt, remote=True):
        output = model.output.save()
    return output


def run_trace_with_sleep(
    model: LanguageModel, sleep_s: float, prompt: str = "Hello world"
):
    """Run a remote trace that blocks server-side for ``sleep_s`` seconds.

    The body executes on the ModelActor under the Protector; ``time`` is
    whitelisted so this works. Useful for "interrupt mid-request" tests.
    """
    with model.trace(prompt, remote=True):
        import time as _t

        _t.sleep(sleep_s)
        output = model.output.save()
    return output


def submit_in_thread(target: Callable, *args, **kwargs) -> threading.Thread:
    """Fire-and-forget a callable in a daemon thread."""
    t = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t
