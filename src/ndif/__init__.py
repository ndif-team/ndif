"""NDIF — the server behind nnsight's ``remote=True``."""

from importlib.metadata import PackageNotFoundError, version as _version

try:
    __version__ = _version("ndif")
except PackageNotFoundError:  # a source tree that was never installed
    __version__ = "0.0.0"
