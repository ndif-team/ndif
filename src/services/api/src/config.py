"""Configuration for the API service."""

# List of packages to include in system_info endpoint
# Set to None to include all installed packages (not recommended for production)
SYSTEM_INFO_PACKAGE_FILTER = [
    "nnsight",
    "ray",
    "fastapi",
    "uvicorn",
    "redis",
    "python-socketio",
    "pydantic",
    "torch",
    "transformers",
    "numpy",
]
