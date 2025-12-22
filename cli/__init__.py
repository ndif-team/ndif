"""NDIF - National Deep Inference Fabric CLI"""
from importlib.metadata import PackageNotFoundError, version
  
try:
    __version__ = version("ndif")
except PackageNotFoundError:
    __version__ = "unknown version"