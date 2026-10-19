"""What a model key says about the model, read without nnsight.

A key is ``"import.path.Class:<suffix>"``; for nnsight's HuggingFace wrappers
the suffix is JSON carrying the repo id, revision, task and -- only when the
client asked for it -- ``trust_remote_code``. Both halves of the server read
the key (the controller to place and load a model, the API and CLI to show
one), so the readers live where both can import them.
"""

from __future__ import annotations

import json

from .types import MODEL_KEY


def trust_remote_code_of(model_key: MODEL_KEY) -> bool:
    """Whether the key asks for the checkpoint's own code to run at load.

    The client wrote ``"trust_remote_code": true`` into the key when it built
    the model with that option; a key without the field is an ordinary
    checkpoint, and so is a suffix that is not JSON at all (a VLLM key).
    """
    try:
        return bool(json.loads(model_key.split(":", 1)[1]).get("trust_remote_code"))
    except (IndexError, ValueError, AttributeError):
        return False
