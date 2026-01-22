"""NDIF CLI Configuration Defaults.

This file contains all configurable environment variables and their default values.
Override any of these by setting the corresponding environment variable.

Example:
    export NDIF_BROKER_URL=redis://localhost:6379/
    export NDIF_RAY_HEAD_PORT=6380
"""

import os

# =============================================================================
# Session Management
# =============================================================================

NDIF_SESSION_ROOT = os.path.expanduser("~/.ndif")

# =============================================================================
# Service URLs
# =============================================================================

NDIF_BROKER_URL = "redis://localhost:6389/"
NDIF_OBJECT_STORE_URL = "http://localhost:27019"
NDIF_API_URL = "http://localhost:8001"
NDIF_RAY_ADDRESS = "ray://localhost:10001"

# =============================================================================
# Service Ports
# =============================================================================

NDIF_BROKER_PORT = 6389
NDIF_OBJECT_STORE_PORT = 27019
NDIF_API_PORT = 8001
NDIF_API_HOST = "0.0.0.0"

# =============================================================================
# Ray Configuration
# =============================================================================

NDIF_RAY_TEMP_DIR = "/tmp/ray"
NDIF_RAY_HEAD_PORT = 6385
NDIF_RAY_CLIENT_PORT = 10001  # For ray:// client connections
NDIF_RAY_DASHBOARD_PORT = 8265
NDIF_RAY_DASHBOARD_HOST = "0.0.0.0"
NDIF_RAY_SERVE_PORT = 8262
NDIF_RAY_OBJECT_MANAGER_PORT = 8076
NDIF_RAY_DASHBOARD_GRPC_PORT = 8268

# =============================================================================
# Controller Configuration
# =============================================================================

NDIF_CONTROLLER_IMPORT_PATH = "src.ray.deployments.controller.controller"
NDIF_MINIMUM_DEPLOYMENT_TIME_SECONDS = 0

# =============================================================================
# API Configuration
# =============================================================================

NDIF_API_WORKERS = 1


# =============================================================================
# Helper: Build ENV_VARS dict from module variables
# =============================================================================

def _build_env_vars():
    """Build ENV_VARS dict from all NDIF_* variables in this module."""
    import sys
    module = sys.modules[__name__]
    env_vars = {}
    for name in dir(module):
        if name.startswith("NDIF_"):
            value = getattr(module, name)
            # Convert to string for environment variable format
            env_vars[name] = str(value)
    return env_vars


ENV_VARS = _build_env_vars()
