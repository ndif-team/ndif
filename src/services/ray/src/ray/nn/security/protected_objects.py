"""Protected object wrappers for model and tokenizer instances.

When a model is loaded by the ModelActor, its torch.nn.Module is wrapped with
``protect(module)`` before being handed to user code.  This prevents users
from:

    - Moving the model between devices — ``.to()``, ``.cuda()``, ``.cpu()``,
      and dtype-changing methods are blocked because they would break the
      shared GPU memory layout.
    - Silently mutating tensor attributes in-place — reads of Tensor, list,
      and dict attributes return deep copies so the original stays intact.
    - Permanently modifying module attributes — writes are tracked in
      SET_ATTRS and reverted by ``clear_set_attrs()`` after each request.

Implementation note: ``protect(obj)`` creates a dynamic subclass of both
ProtectedObject and ``obj.__class__``.  This way ``isinstance(wrapped, Module)``
still returns True, keeping downstream code (nnsight, accelerate, etc.) happy.
"""

from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
from typing import Any

import torch

# Maps ``id(wrapper)`` → original unwrapped object.
PROTECTIONS: dict[int, Any] = {}

# Tracks attribute writes so they can be rolled back.
# Maps ``id(wrapper)`` → { attr_name: original_value }.
SET_ATTRS: defaultdict[int, dict[str, Any]] = defaultdict(dict)

# Methods that move the model between devices or change its dtype.
# Blocked because the model is shared across requests and pinned to
# specific GPUs by the cluster scheduler.
_BLOCKED_METHODS = frozenset({
    # Device movement
    "to",
    "cuda",
    "cpu",
    "xpu",
    "ipu",
    "to_empty",
    # Dtype changes (these return a new module / modify in-place)
    "half",
    "float",
    "double",
    "bfloat16",
    # Gradient state mutation
    "requires_grad_",
})


def protected(obj: Any) -> bool:
    """True if *obj* is a ProtectedObject that has finished __init__."""
    return id(obj) in PROTECTIONS


class ProtectedObject:

    def __init__(self, obj: Any):
        PROTECTIONS[id(self)] = obj

    def __getattribute__(self, name: str):
        if name in _BLOCKED_METHODS:
            raise ValueError(
                f"Method `{name}` cannot be called on a protected object"
            )

        obj = PROTECTIONS[id(self)]
        value = getattr(obj, name)

        # Return deep copies of mutable types so users can't silently mutate
        # the model's internal state (e.g. bias vectors, config dicts).
        if isinstance(value, (torch.Tensor, list, dict)):
            value = deepcopy(value)
            print(
                f" WARNING: Accessing attribute `{name}` of protected object"
                f" `{PROTECTIONS[id(self)]}` will return a deepcopy of the attribute."
            )

        return value

    def __getattr__(self, name: str):
        raise AttributeError(f"Attribute `{name}` cannot be accessed")

    def __setattr__(self, name: str, value: Any):
        if not protected(self):
            # Still inside __init__ — allow normal attribute setting.
            object.__setattr__(self, name, value)
        else:
            # Record the original value so clear_set_attrs() can revert it.
            SET_ATTRS[id(self)][name] = getattr(PROTECTIONS[id(self)], name)
            PROTECTIONS[id(self)].__dict__[name] = value


def protect(obj: Any):
    """Wrap *obj* in a ProtectedObject that also inherits from obj's class."""
    class _ProtectedObject(ProtectedObject, obj.__class__):
        pass
    return _ProtectedObject(obj)


def clear_set_attrs():
    """Revert all attribute writes made to protected objects.

    Called by the ModelActor after each request to ensure one user's
    modifications don't leak into the next request.
    """
    for obj in list(PROTECTIONS.values()):
        for name in SET_ATTRS[id(obj)]:
            setattr(obj, name, SET_ATTRS[id(obj)][name])
        SET_ATTRS[id(obj)].clear()
