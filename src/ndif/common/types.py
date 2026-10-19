"""Shared type aliases used across services."""

# A model identifier (e.g. "nnsight.modeling.LanguageModel:openai-community/gpt2").
MODEL_KEY = str

# A replica identifier, unique across the cluster.
REPLICA_ID = str

# A Ray node identifier.
NODE_ID = str


def key_field(model_key: MODEL_KEY, name: str):
    """A field of the key's JSON suffix (``task``, ``trust_remote_code``, ...).

    ``None`` when the suffix has no such field or is not JSON (a VLLM key).
    """
    import json

    try:
        return json.loads(model_key.split(":", 1)[1]).get(name)
    except (IndexError, ValueError, AttributeError):
        return None
