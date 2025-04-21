import os
import time
from concurrent.futures import TimeoutError
from contextlib import AbstractContextManager
from functools import wraps
from typing import Callable

import torch
from torch.overrides import TorchFunctionMode

from nnsight.util import Patch, Patcher
from nnsight.schema.format import functions
from nnsight.tracing.graph import Graph, Node

def get_total_cudamemory_MBs(return_ids=False) -> int:

    cudamemory = 0

    ids = []

    for device in range(torch.cuda.device_count()):
        try:
            cudamemory += torch.cuda.mem_get_info(device)[1] * 1e-6
            if return_ids:
                ids.append(device)
        except:
            pass

    if return_ids:

        return int(cudamemory), ids

    return int(cudamemory)


def set_cuda_env_var(ids=None):

    del os.environ["CUDA_VISIBLE_DEVICES"]

    if ids == None:

        _, ids = get_total_cudamemory_MBs(return_ids=True)

    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join([str(x) for x in ids])


def update_nnsight_print_function(new_function):

    new_function.__name__ = functions.get_function_name(print)

    functions.FUNCTIONS_WHITELIST[functions.get_function_name(print)] = new_function


class NNsightTimer(AbstractContextManager):

    class FunctionMode(TorchFunctionMode):

        def __init__(self, timer: "NNsightTimer"):

            self.timer = timer

            super().__init__()

        def __torch_function__(self, func, types, args=(), kwargs=None):
            
            self.timer.check()

            if kwargs is None:
                kwargs = {}
            
            return func(*args, **kwargs)

    def __init__(self, timeout: float):

        self.timeout = timeout
        self.start: float = None

        self.patcher = Patcher(
            [
                Patch(Node, self.wrap(Node.execute), "execute"),
                Patch(Graph, self.wrap(Graph.execute), "execute"),
            ]
        )

        self.fn_mode = NNsightTimer.FunctionMode(self)

    def __enter__(self):
        
        if self.timeout is not None:

            self.reset()

            self.patcher.__enter__()
            self.fn_mode.__enter__()

        return self

    def __exit__(self, exc_type, exc_value, traceback):
        
        if self.timeout is not None:

            self.patcher.__exit__(None, None, None)
            self.fn_mode.__exit__(None, None, None)

        if isinstance(exc_value, Exception):
            raise exc_value

    def reset(self):

        self.start = time.time()

    def wrap(self, fn: Callable):

        @wraps(fn)
        def inner(*args, **kwargs):

            self.check()

            return fn(*args, **kwargs)

        return inner

    def check(self):

        if self.start and time.time() - self.start > self.timeout:
            
            self.start = 0
            
            raise Exception(
                f"Job took longer than timeout: {self.timeout} seconds"
            )