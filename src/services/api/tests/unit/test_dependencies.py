from unittest.mock import patch
from src.dependencies import check_hotswapping_access, validate_nnsight_version, validate_python_version, authenticate_api_key
import pytest
import os


@pytest.mark.asyncio
async def test_validate_python_version():
    with patch("sys.version", "3.12.10"):
        res = await validate_python_version("3.12")
        assert res == "3.12"


@pytest.mark.asyncio
async def test_validate_nnsight_version():
    os.environ["MIN_NNSIGHT_VERSION"] = "0.5.10"
    res = await validate_nnsight_version("0.5.10")
    assert res == "0.5.10"


@pytest.mark.asyncio
async def test_authenticate_api_key_dev_mode():
    os.environ["DEV_MODE"] = "true"
    res = await authenticate_api_key("test-key")
    assert res == "test-key"


@pytest.mark.asyncio
async def test_check_hotswapping_access_dev_mode():
    os.environ["DEV_MODE"] = "true"
    res = await check_hotswapping_access("test-key")
    assert res == True
