import asyncio
import uuid

from fastapi import HTTPException, Request
from opentelemetry import trace
from starlette.status import (
    HTTP_400_BAD_REQUEST,
    HTTP_401_UNAUTHORIZED,
    HTTP_403_FORBIDDEN,
    HTTP_503_SERVICE_UNAVAILABLE,
)

from .config import AppConfig
from ...common.types import API_KEY, TIER
from .db import api_key_store
from ...common.schema.request import BackendRequestModel
from ...common.providers.redis import RedisProvider
from ...common.tracing import trace_span


async def authenticate_api_key(api_key: API_KEY) -> API_KEY:
    """Authenticate API key.

    Args:
        api_key: API key to authenticate.

    Returns:
        The validated API key string.

    Raises:
        HTTPException: If the API key is missing or invalid, or validation is not configured.
    """
    if AppConfig.dev_mode:
        return api_key

    if api_key_store is None:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="API key validation is not configured.",
        )

    # Validate API key format before checking database
    try:
        uuid.UUID(api_key, version=4)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Invalid API key format: '{api_key}'. "
            f"API keys must be in the format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx. "
            f"You can obtain a valid API key from https://login.ndif.us",
        )

    if not await asyncio.to_thread(api_key_store.api_key_exists, api_key):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Please visit https://login.ndif.us/ to create a new one.",
        )

    # Competition access control: only keys holding the `tier_1` tier may use
    # NDIF. A valid-but-untiered key is rejected outright.
    if not await asyncio.to_thread(
        api_key_store.key_has_tier, api_key, TIER.TIER_1
    ):
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="Your API key is not authorized to use NDIF for this competition. "
            "A `tier_1` tier is required.",
        )

    return api_key


async def validate_python_version(python_version: str) -> str:
    """Validate Python version compatibility.

    Args:
        python_version: Python version to validate.

    Returns:
        The validated Python version string.

    Raises:
        HTTPException: If the Python version is missing or incompatible.
    """

    user_python_version = ".".join(python_version.split(".")[0:2])

    if user_python_version == "":
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Client python version was not provided to the NDIF server. This likely means that you are using an outdated version of nnsight. Please update your nnsight version and try again.",
        )

    from packaging.version import Version

    user_version = Version(user_python_version)

    if user_version < AppConfig.min_python_version_parsed:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Client python version {user_python_version} is incompatible with the server. The minimum supported version is {AppConfig.min_python_version_parsed}. Please update your python version and try again.",
        )

    return user_python_version


async def validate_nnsight_version(nnsight_version: str) -> str:
    """Validate nnsight version compatibility.

    Args:
        nnsight_version: nnsight version to validate.

    Returns:
        The validated nnsight version string.

    Raises:
        HTTPException: If the nnsight version is missing or incompatible.
    """

    if AppConfig.dev_mode:
        return nnsight_version

    if nnsight_version == "":
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Client nnsight version was not provided to the NDIF server. This likely means that you are using an outdated version of nnsight. Please update your nnsight version and try again.",
        )

    from packaging.version import Version

    user_nnsight_version = Version(nnsight_version)

    if user_nnsight_version < AppConfig.min_nnsight_version_parsed:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Client nnsight version {user_nnsight_version} is incompatible with the server nnsight version. The minimum supported version is {AppConfig.min_nnsight_version_parsed}. Please update nnsight to the latest version: `pip install --upgrade nnsight`",
        )

    return nnsight_version


async def check_hotswapping_access(api_key: API_KEY) -> bool:
    """Check if the API key has hotswapping access.

    Args:
        api_key: The API key to check.

    Returns:
        True if hotswapping is enabled for this API key, False otherwise.
    """
    if AppConfig.dev_mode:
        return True
    if api_key_store is None:
        return False
    return await asyncio.to_thread(api_key_store.key_has_hotswapping_access, api_key)


async def get_email(api_key: API_KEY) -> str | None:
    """Look up the email associated with an API key.

    Args:
        api_key: The API key to resolve.

    Returns:
        The associated email, or None if the key is unknown / unconfigured.
    """
    if AppConfig.dev_mode or api_key_store is None:
        return None
    return await asyncio.to_thread(api_key_store.get_email_from_key, api_key)


async def require_ray_connection() -> None:
    """FastAPI dependency to ensure Ray is connected before processing.

    Checks the 'ray:connected' Redis key which is maintained by the Dispatcher.
    If Ray is not connected, returns a 503 Service Unavailable error.

    Raises:
        HTTPException: 503 if Ray is not connected.
    """
    is_connected = await RedisProvider.async_client.get("ray:connected")

    if not is_connected:
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable: compute backend is reconnecting. Please try again in a few minutes.",
        )


async def validate_request(raw_request: Request) -> BackendRequestModel:
    """FastAPI dependency to validate and create a BackendRequestModel.

    This dependency:
    1. Validates the API key
    2. Validates NNSight version compatibility
    3. Validates Python version compatibility
    4. Creates a BackendRequestModel from the raw request
    5. Populates the hotswapping field

    Args:
        raw_request: The raw FastAPI Request object.

    Returns:
        A fully validated BackendRequestModel ready for processing.

    Raises:
        HTTPException: If any validation fails.
    """
    with trace_span("api.validate_request") as span:
        # Extract values from headers
        api_key = raw_request.headers.get("ndif-api-key", "")
        nnsight_version = raw_request.headers.get("nnsight-version", "")
        python_version = raw_request.headers.get("python-version", "")

        span.set_attribute("ndif.client.nnsight_version", nnsight_version)
        span.set_attribute("ndif.client.python_version", python_version)

        try:
            # Validate using existing dependency functions (call them directly, not as dependencies)
            await authenticate_api_key(api_key)
            await validate_nnsight_version(nnsight_version)
            await validate_python_version(python_version)

            # Create BackendRequestModel
            backend_request = BackendRequestModel.from_request(raw_request)

            # Populate hotswapping access
            backend_request.hotswapping = await check_hotswapping_access(api_key)

            span.set_attribute("ndif.request.id", str(backend_request.id))
            if backend_request.model_key:
                span.set_attribute("ndif.model.key", str(backend_request.model_key))

            return backend_request

        except Exception as e:
            span.set_status(trace.StatusCode.ERROR, str(e))
            span.record_exception(e)
            raise
