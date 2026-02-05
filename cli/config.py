"""NDIF CLI Configuration Defaults.

This file contains all configurable environment variables and their default values.
Override any of these by setting the corresponding environment variable.

Example:
    export NDIF_BROKER_URL=redis://localhost:6379/
    export NDIF_RAY_HEAD_PORT=6380
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Find project root (where .env.example lives)
_PROJECT_ROOT = Path(__file__).parent.parent

# Load .env.example for defaults, then .env for overrides
load_dotenv(_PROJECT_ROOT / ".env.example")
load_dotenv(_PROJECT_ROOT / ".env", override=True)

# =============================================================================
# Session Management
# =============================================================================

NDIF_SESSION_ROOT = os.path.expanduser("~/.ndif")


# =============================================================================
# Helper: Build ENV_VARS dict from module variables
# =============================================================================

def _build_env_vars():
    """Build ENV_VARS dict from NDIF_* variables in this module and os.environ."""
    module = sys.modules[__name__]
    env_vars = {}
    # First, get from os.environ (includes vars loaded from .env files)
    for name, value in os.environ.items():
        if name.startswith("NDIF_"):
            env_vars[name] = value
    # Then, override with module-level defaults (these are fallbacks)
    for name in dir(module):
        if name.startswith("NDIF_") and name not in env_vars:
            value = getattr(module, name)
            env_vars[name] = str(value)
    return env_vars


ENV_VARS = _build_env_vars()
