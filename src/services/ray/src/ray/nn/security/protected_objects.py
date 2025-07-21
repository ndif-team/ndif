from __future__ import annotations

from typing import Any
from nnsight.intervention.envoy import Envoy
from copy import deepcopy
from nnsight.intervention.tracing.base import Tracer
PROTECTION = set()
ALLOWED = set()  
        

def allow(func):

    def inner(self, *args, **kwargs):
        
        with UnprotectContext(id(self)):
            __defer_capture__ = True
            return func(self, *args, **kwargs)
        
        
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
    
    __allowed_attr__ = ()
    __allowed_setattr__ = ()
    __allowed_types__ = ()
    
    def __init__(self, obj: Any):
        
        self.__dict__ = obj.__dict__.copy()
        self.__obj__ = obj
        
        PROTECTION.add(id(self))


    @allow
    def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)
        
        
    def __getattribute__(self, name: str):
        
        value = object.__getattribute__(self, name)
       
        if protected(self) and not (name in type(self).__allowed_attr__ or isinstance(value, ProtectedObject) or isinstance(value, type(self).__allowed_types__) or allowed(value)):
            raise ValueError(f"Attribute '{name}' is not allowed")
       
        return value
    
    def __setattr__(self, name: str, value: Any):
   
        if not protected(self) or name in type(self).__allowed_setattr__:
            object.__setattr__(self, name, value)
        else:
            raise AttributeError(f"Attribute '{name}' cannot be set after initialization")
        
class ProtectedEnvoy(ProtectedObject, Envoy):
    
    __allowed_attr__ = ("output", "inputs", "input", "device")
    __allowed_setattr__ = ("output", "inputs", "input")
    __allowed_types__ = (Tracer,)    
        
    def __init__(self, envoy: Envoy):
        ProtectedObject.__init__(self, envoy)
        
        with UnprotectContext(id(self)):
            
            for key, value in self.__dict__.items():
                if isinstance(value, Envoy) and key != "__obj__":
                    self.__dict__[key] = ProtectedEnvoy(value)
        
    def __getattr__(self, name: str):
        with UnprotectContext(id(self)):    
            value = self.__obj__.__getattr__(name)
            
            if isinstance(value, Tracer):
                #TODO Protect
                return value
            
            return deepcopy(value)
        
        

    @allow
    def trace(self, *args, **kwargs):
        __defer_capture__ = False
        return self.__obj__.trace(*args, **kwargs)
    
    @property
    @allow
    def output(self):
        return self.__obj__.output
    
    @output.setter
    @allow
    def output(self, value: Any):
        self.__obj__.output = value
    
    @property
    @allow
    def inputs(self):
        return self.__obj__.inputs
    
    @inputs.setter
    @allow
    def inputs(self, value: Any):
        self.__obj__.inputs = value
    
    @property
    @allow
    def input(self):
        return self.__obj__.input
    
    @input.setter
    @allow
    def input(self, value: Any):
        self.__obj__.input = value
    
    @property
    @allow
    def device(self):
        return self.__obj__.device
    
    @allow
    def get(self, *args, **kwargs):
        return Envoy.get(self, *args, **kwargs)
    
    @allow
    def skip(self, *args, **kwargs):
        return self.__obj__.skip(*args, **kwargs)
    
    @allow
    def wait_for_input(self, *args, **kwargs):
        return self.__obj__.wait_for_input(*args, **kwargs)
    
    @allow
    def wait_for_output(self, *args, **kwargs):
        return self.__obj__.wait_for_output(*args, **kwargs)
    
    @allow
    def __str__(self):
        return super().__str__()
    
    @allow
    def __repr__(self):
        return super().__repr__()

        
    
def protect(obj: Any):
    
    if isinstance(obj, Envoy):
        class _ProtectedEnvoy(ProtectedEnvoy, obj.__class__):
            pass
        return _ProtectedEnvoy(obj)
    
    return obj
        
# from nnsight import Envoy
# import torch
# from collections import OrderedDict

# input_size = 5
# hidden_dims = 10
# output_size = 2



# net = torch.nn.Sequential(
#     OrderedDict(
#         [
#             ("layer1", torch.nn.Linear(input_size, hidden_dims)),
#             ("layer2", torch.nn.Linear(hidden_dims, output_size)),
#         ]
#     )
# )

# net = Envoy(net).cuda()


# tiny_input =  torch.rand((1, input_size))

# net = ProtectedEnvoy(net)


# with net.trace(tiny_input):
#     output = net.layer1.inputs.shape.save()
        
# print(output)
    
    
    
    
    
    
    