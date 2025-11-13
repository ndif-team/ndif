import os
import sys

from fastapi import HTTPException, Request
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_401_UNAUTHORIZED
from packaging.version import Version
from nnsight import __version__

from .types import API_KEY
from .db import api_key_store
from .schema import BackendRequestModel

MIN_NNSIGHT_VERSION = os.getenv('MIN_NNSIGHT_VERSION', __version__)
DEV_MODE = os.environ.get("DEV_MODE", "false").lower() == "true"


async def authenticate_api_key(api_key: API_KEY) -> API_KEY:
    """Authenticate API key.

    Args:
        api_key: API key to authenticate.

    Returns:
        The validated API key string.

    Raises:
        HTTPException: If the API key is missing or invalid, or validation is not configured.
    """
    if DEV_MODE:
        return api_key

    if api_key_store is None:
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="API key validation is not configured.",
        )

    if not api_key_store.api_key_exists(api_key):
        raise HTTPException(
            status_code=HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API key. Please visit https://login.ndif.us/ to create a new one.",
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
    server_python_version = '.'.join(sys.version.split('.')[0:2])  # e.g. 3.12
    user_python_version = '.'.join(python_version.split('.')[0:2])

    if user_python_version == '':
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Client python version was not provided to the NDIF server. This likely means that you are using an outdated version of nnsight. Please update your nnsight version and try again."
        )

    if user_python_version != server_python_version:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Client python version {user_python_version} does not match server version: {server_python_version}. Please update your python version and try again."
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
    if nnsight_version == '':
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail="Client nnsight version was not provided to the NDIF server. This likely means that you are using an outdated version of nnsight. Please update your nnsight version and try again."
        )

    min_nnsight_version = Version(MIN_NNSIGHT_VERSION)
    user_nnsight_version = Version(nnsight_version)

    if user_nnsight_version < min_nnsight_version:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Client nnsight version {user_nnsight_version} is incompatible with the server nnsight version. The minimum supported version is {min_nnsight_version}. Please update nnsight to the latest version: `pip install --upgrade nnsight`"
        )

    return nnsight_version


async def check_hotswapping_access(api_key: API_KEY) -> bool:
    """Check if the API key has hotswapping access.

    Args:
        api_key: The API key to check.

    Returns:
        True if hotswapping is enabled for this API key, False otherwise.
    """
    if DEV_MODE:
        return True
    if api_key_store is None:
        return False
    return api_key_store.key_has_hotswapping_access(api_key)


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
    # Extract values from headers
    api_key = raw_request.headers.get("ndif-api-key", "")
    nnsight_version = raw_request.headers.get("nnsight-version", "")
    python_version = raw_request.headers.get("python-version", "")

    # Validate using existing dependency functions (call them directly, not as dependencies)
    await authenticate_api_key(api_key)
    await validate_nnsight_version(nnsight_version)
    await validate_python_version(python_version)

    # Create BackendRequestModel
    backend_request = BackendRequestModel.from_request(raw_request)

    # Populate hotswapping access
    backend_request.hotswapping = await check_hotswapping_access(api_key)

    return backend_request
