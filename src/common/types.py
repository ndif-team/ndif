import uuid
from enum import Enum
from typing import TypeAlias, Union

HF_REPO_ID: TypeAlias = str
"""Hugging Face repository ID.

Example:
openai-community/gpt2
"""

MODEL_KEY: TypeAlias = str
"""Model key identifier. Used to specify the NNsight class, huggingface repo and revision for a deployment.

Example:
nnsight.modeling.language.LanguageModel:{"repo_id": "openai-community/gpt2", "revision": "main"}
"""

RAY_APP_NAME: TypeAlias = str
"""Ray application name. Can be derived directly from MODEL_KEY

Example:
Model:nnsight-modeling-language-languagemodel-repo-id-openai-community-gpt2-revision-main
"""

API_KEY: TypeAlias = str

REQUEST_ID: TypeAlias = str

SESSION_ID: TypeAlias = str

NODE_ID: TypeAlias = str


class TIER(Enum):
    """Tier identifier for API keys"""
    TIER_405B = "405b"
    TIER_HOTSWAP = "hotswapping"