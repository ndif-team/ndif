"""Shared type aliases used across services."""

# A model identifier (e.g. "nnsight.modeling.LanguageModel:openai-community/gpt2").
MODEL_KEY = str

# A replica identifier, unique across the cluster.
REPLICA_ID = str

# A Ray node identifier.
NODE_ID = str
