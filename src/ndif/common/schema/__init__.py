"""Schemas shared across NDIF services.

`deployment_config` is cheap to import (pydantic + types only), so it's
re-exported eagerly. The rest (`request`, `response`, `result`, `mixins`)
transitively pull in nnsight/transformers/diffusers — ~7s of import — so
they are exposed through PEP 562 lazy attribute access. Consumers keep
using `from ndif.common.schema import BackendRequestModel` unchanged;
the module only loads when a lazy name is first accessed. This keeps
the CLI fast for commands that don't actually need the request schemas.
"""
from .deployment_config import DeploymentConfig

__all__ = [
    "DeploymentConfig",
    "BackendRequestModel",
    "BackendResponseModel",
    "BackendResultModel",
    "ObjectStorageMixin",
    "TelemetryMixin",
]

_LAZY_ATTRS = {
    "BackendRequestModel": "request",
    "BackendResponseModel": "response",
    "BackendResultModel": "result",
    "ObjectStorageMixin": "mixins",
    "TelemetryMixin": "mixins",
}


def __getattr__(name):
    submod = _LAZY_ATTRS.get(name)
    if submod is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module
    module = import_module(f".{submod}", __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
