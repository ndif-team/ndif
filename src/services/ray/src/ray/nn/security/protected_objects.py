from __future__ import annotations

from copy import deepcopy
from collections import defaultdict
from typing import Any

import torch


PROTECTIONS = {}
SET_ATTRS = defaultdict(dict)


def protected(obj: Any):
    return id(obj) in PROTECTIONS


class ProtectedObject:
    def __init__(self, obj: Any):
        PROTECTIONS[id(self)] = obj

    def __getattribute__(self, name: str):
        if name in ["to"]:
            raise ValueError(f"Attribute `{name}` cannot be accessed")

        obj = PROTECTIONS[id(self)]

        value = getattr(obj, name)

        if not isinstance(value, (torch.Tensor, list, dict)):
            return value

        value = deepcopy(value)

        print(
            f" WARNING: Accessing attribute `{name}` of protected object `{PROTECTIONS[id(self)]}` will return a deepcopy of the attribute."
        )

        return value

    def __getattr__(self, name: str):
        raise AttributeError(f"Attribute `{name}` cannot be accessed")

    def __setattr__(self, name: str, value: Any):
        if not protected(self):
            object.__setattr__(self, name, value)
        else:
            SET_ATTRS[id(self)][name] = getattr(PROTECTIONS[id(self)], name)
            PROTECTIONS[id(self)].__dict__[name] = value


def protect(obj: Any):
    class _ProtectedObject(ProtectedObject, obj.__class__):
        pass

    return _ProtectedObject(obj)


def clear_set_attrs():
    for obj in list(PROTECTIONS.values()):
        for name in SET_ATTRS[id(obj)]:
            setattr(obj, name, SET_ATTRS[id(obj)][name])
        SET_ATTRS[id(obj)].clear()
