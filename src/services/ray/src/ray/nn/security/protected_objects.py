from __future__ import annotations

import pickle
from copy import deepcopy
from typing import Any

import torch

from nnsight.intervention import serialization
from nnsight.intervention.envoy import Envoy
from nnsight.util import Patch, Patcher

PROTECTIONS = {}


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

    def __setattr__(self, name: str, value: Any):
        if not protected(self):
            object.__setattr__(self, name, value)
        else:
            raise AttributeError(
                f"Attribute '{name}' cannot be set after initialization"
            )


def protect(obj: Any):
    class _ProtectedObject(ProtectedObject, obj.__class__):
        pass

    return _ProtectedObject(obj)


original_setstate = Envoy.__setstate__


class ProtectedCustomCloudUnpickler(serialization.CustomCloudUnpickler):
    def load(self):
        def inject(_self, state):
            original_setstate(_self, state)

            envoy = self.root.get(_self.path.removeprefix("model"))

            module = protect(envoy._module)

            _self._module = module
            _self._interleaver = envoy._interleaver

            for key, value in envoy.__dict__.items():
                if key not in _self.__dict__:
                    _self.__dict__[key] = value

        with Patcher([Patch(Envoy, inject, "__setstate__")]):
            return pickle.Unpickler.load(self)


def protect_model():
    serialization.CustomCloudUnpickler = ProtectedCustomCloudUnpickler
