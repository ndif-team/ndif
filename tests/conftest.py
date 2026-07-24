"""Shared fixtures for the remote nnsight tests.

These run the *local* (vendored, ``./nnsight``) client against a local NDIF
server at ``http://localhost:8001``, exercising the ``remote=True`` path end to
end: the client serializes the traced block, the server deploys the model and
runs it, and saved values come back over the wire.

The host is set on ``nnsight.CONFIG`` at import (before any trace builds a remote
backend). The whole suite skips if no server is reachable, so it's safe to
collect without one. ``openai-community/gpt2`` is used because the dev server
provisions it (a tiny random model failed to provision).
"""

import importlib.util
import urllib.request

import pytest

import nnsight

HOST = "http://localhost:8001"

# Point the client at the local server before anything builds a RemoteBackend
# (which reads CONFIG.API.HOST). Quiet the status spinner so pytest output stays
# readable.
nnsight.CONFIG.API.HOST = HOST
nnsight.CONFIG.APP.REMOTE_LOGGING = False

# gpt2 dimensions, for shape assertions.
REPO = "openai-community/gpt2"
PROMPT = "The quick brown fox jumps over"
HIDDEN = 768
VOCAB = 50257

# A small, hub-hosted LoRA adapter over gpt2 (the server downloads it by id; a
# local adapter path wouldn't be visible inside the model container).
PEFT_ADAPTER = "peft-internal-testing/gpt2-lora-random"

# The client needs peft to graft the adapter's architecture onto its meta model
# (matching the paths the server's adapted model exposes).
peft_installed = importlib.util.find_spec("peft") is not None


def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{HOST}/ping", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


SERVER_UP = _server_up()

# Every test in this suite needs the live server; skip the lot if it's down.
requires_server = pytest.mark.skipif(
    not SERVER_UP, reason=f"no NDIF server reachable at {HOST}"
)


@pytest.fixture(scope="module")
def model():
    """An undispatched gpt2 — the server loads and runs the real weights; the
    client only needs the architecture to build the request. Module-scoped: the
    remote model stays deployed across tests, so only the first pays deploy time."""
    from nnsight.modeling.transformers import TransformersModel

    return TransformersModel(REPO, task="text-generation")
