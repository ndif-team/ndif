from __future__ import annotations

from typing import Any
from nnsight.intervention.envoy import Envoy
from functools import wraps

PROTECTION = set()
ALLOWED = set()
class ProtectedMethod:
    
         
    def __getattribute__(self, name: str):
        
        if name not in ["__call__"]:
            raise ValueError(f"Attribute '{name}' is not allowed")
        
        return object.__getattribute__(self, name)
    
    def __setattr__(self, name: str, value: Any):
   
        raise ValueError(f"Attribute '{name}' is not allowed")
        
        

def allow(func):

    # class InnerProtectedMethod(ProtectedMethod):
        
    #     def __get__(self, instance, owner):
    #         if instance is None:
    #             return self
    #         # Return a bound method: we return a new callable that inserts self (the instance)
    #         def bound_method(*args, **kwargs):
    #             return func(instance, *args, **kwargs)
    #         return bound_method
    
    
    @wraps(func)
    def inner(self, *args, **kwargs):
        
        with UnprotectContext(id(self)):
            return func(self, *args, **kwargs)
        
    
    del inner.__closure__
    del inner.__wrapped__
    del inner.__globals__
        
    PROTECTION.add(id(inner))
    ALLOWED.add(id(inner))
        
    return inner


def protected(obj:Any):
    
    return id(obj) in PROTECTION

def allowed(obj:Any):
    if hasattr(obj, "__func__"):
        obj = obj.__func__
    return id(obj) in ALLOWED


class UnprotectContext:
    """Context manager that temporarily removes an object from PROTECTION."""
    
    def __init__(self, obj_id: int):
        self.obj_id = obj_id
        self.was_protected = False
        
    def __enter__(self):
        self.was_protected = self.obj_id in PROTECTION
        if self.was_protected:
            PROTECTION.remove(self.obj_id)
        return self.obj_id
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.was_protected:
            PROTECTION.add(self.obj_id)



class ProtectedObject:
    
    __allowed_attr__ = None
    
    def __init__(self, obj: Any):
        
        self.__dict__ = obj.__dict__.copy()
        
        PROTECTION.add(id(self))


    @allow
    def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)
        
        
    def __getattribute__(self, name: str):
        
        value = object.__getattribute__(self, name)
       
        if protected(self) and not allowed(value):
            breakpoint()
            raise ValueError(f"Attribute '{name}' is not allowed")
       
        return value
        
    def __getattr__(self, name: str):
        
        
        try:
            value = object.__getattr__(self, name)
        except AttributeError:

            raise AttributeError(f"Attribute '{name}' is not allowed")
       
        if protected(self) and not allowed(value):
            
            raise ValueError(f"Attribute '{name}' is not allowed")
            
        return value
    
    def __setattr__(self, name: str, value: Any):
   
        if not protected(self):
            object.__setattr__(self, name, value)
        else:
            raise AttributeError(f"Attribute '{name}' cannot be set after initialization")
        
class ProtectedEnvoy(ProtectedObject, Envoy):
    
    def __init__(self, envoy: Envoy):
        ProtectedObject.__init__(self, envoy)
        
    @allow
    def cpu(self):
        return super().cpu()
    
    @allow
    def cuda(self):
        return super().cuda()
    
    @allow
    def __str__(self):
        return super().__str__()
    
    @allow
    def __repr__(self):
        return super().__repr__()
    
        
        
from collections import OrderedDict

import torch

from nnsight import Envoy

input_size = 5
hidden_dims = 10
output_size = 2



net = torch.nn.Sequential(
    OrderedDict(
        [
            ("layer1", torch.nn.Linear(input_size, hidden_dims)),
            ("layer2", torch.nn.Linear(hidden_dims, output_size)),
        ]
    )
)

net = Envoy(net).cuda()


tiny_input =  torch.rand((1, input_size))

net = ProtectedEnvoy(net)
net.cpu()
breakpoint()