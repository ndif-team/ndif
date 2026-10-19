"""Shared type aliases used across services."""

# A model identifier (e.g. "nnsight.modeling.LanguageModel:openai-community/gpt2").
MODEL_KEY = str

# A replica identifier, unique across the cluster.
REPLICA_ID = str

# A Ray node identifier.
NODE_ID = str


def trust_remote_code_of(model_key: MODEL_KEY) -> bool:
    """Whether the key asks for the checkpoint's own code to run at load.

    nnsight writes ``"trust_remote_code": true`` into a HuggingFace key's JSON
    suffix when the model was built with it, and omits the field otherwise. A
    suffix that is not JSON (a VLLM key) is an ordinary checkpoint.
    """
    import json

    try:
        return bool(json.loads(model_key.split(":", 1)[1]).get("trust_remote_code"))
    except (IndexError, ValueError, AttributeError):
        return False
