import uuid
from enum import Enum
from typing import Optional
from slugify import slugify


class MODEL_KEY(str):
    """Model key identifier with validation.

    Example:
    nnsight.modeling.language.LanguageModel:{"repo_id": "openai-community/gpt2", "revision": "main"}
    """
    def __new__(cls, value: str) -> 'MODEL_KEY':
        if not value or not isinstance(value, str):
            raise ValueError(f"MODEL_KEY must be a non-empty string, got: {value}")
        return str.__new__(cls, value.strip())


class HF_REPO_ID(str):
    """Hugging Face repository ID.

    Example:
    openai-community/gpt2
    """
    def __new__(cls, value: str) -> 'HF_REPO_ID':
        if not value or not isinstance(value, str):
            raise ValueError(f"HF_REPO_ID must be a non-empty string, got: {value}")
        value = value.strip()
        if '/' not in value:
            raise ValueError(f"HF_REPO_ID must contain '/' (org/model format), got: {value}")
        return str.__new__(cls, value)


class RAY_APP_NAME(str):
    """Ray application name.
    
    Example:
    Model:nnsight-modeling-language-languagemodel-repo-id-openai-community-gpt2-revision-main
    """
    def __new__(cls, value) -> 'RAY_APP_NAME':
        # If a MODEL_KEY is passed, automatically convert it
        if isinstance(value, MODEL_KEY):
            value = RAY_APP_NAME._from_model_key(value)
        elif not value or not isinstance(value, str):
            raise ValueError(f"RAY_APP_NAME must be a non-empty string or MODEL_KEY, got: {value}")

        return str.__new__(cls, str(value).strip())

    @classmethod
    def _from_model_key(cls, model_key: MODEL_KEY) -> 'RAY_APP_NAME':
        """Create RAY_APP_NAME from MODEL_KEY using slugify"""
        return f"Model:{slugify(model_key)}"

class TIER(Enum):
    """Tier identifier for API keys"""
    TIER_405B = "405b"
    TIER_HOTSWAP = "hotswapping"


class API_KEY(str):
    """API key identifier with validation"""
    def __new__(cls, value: Optional[str] = None) -> 'API_KEY':
        if value is None:
            value = ""
        elif not isinstance(value, str):
            raise ValueError(f"API_KEY must be a string, got: {type(value)}")
        return str.__new__(cls, value.strip())


class REQUEST_ID(str):
    """Request ID identifier with UUID validation"""
    def __new__(cls, value: Optional[str] = None) -> 'REQUEST_ID':
        if value is None:
            value = str(uuid.uuid4())
        elif isinstance(value, uuid.UUID):
            value = str(value)
        elif not isinstance(value, str):
            raise ValueError(f"REQUEST_ID must be a string or UUID, got: {type(value)}")

        value = value.strip()
        # Validate UUID format
        try:
            uuid.UUID(value)
        except ValueError:
            raise ValueError(f"REQUEST_ID must be a valid UUID, got: {value}")

        return str.__new__(cls, value)


class SESSION_ID(str):
    """Session ID identifier"""
    def __new__(cls, value: str) -> 'SESSION_ID':
        if not isinstance(value, str):
            raise ValueError(f"SESSION_ID must be a string, got: {type(value)}")
        return str.__new__(cls, value.strip())


class NODE_ID(str):
    """Node ID identifier"""
    def __new__(cls, value: str) -> 'NODE_ID':
        if not value or not isinstance(value, str):
            raise ValueError(f"NODE_ID must be a non-empty string, got: {value}")
        return str.__new__(cls, value.strip())