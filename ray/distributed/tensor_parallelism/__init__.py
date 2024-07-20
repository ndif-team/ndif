from functools import partial

import torch
from torch.distributed._tensor.api import DTensor
from torch.distributed.tensor.parallel import ParallelStyle, parallelize_module as _parallelize_module

from .plans import model_id_to_plans


def ready_to_be_parallelized(module: torch.nn.Module):

    param = next(module.parameters())

    return not isinstance(param, DTensor) and param.device.type != 'meta' and param.nonzero().numel() != 0


def parallelize_on_state_dict_load(plan, module: torch.nn.Module, tp_mesh):

    def parallelize_hook(plan, module: torch.nn.Module, keys):

        if ready_to_be_parallelized(module):

            _parallelize_module(module, tp_mesh, plan)

    return module.register_load_state_dict_post_hook(partial(parallelize_hook, plan))


def parallelize_module(module: torch.nn.Module, module_path: str, plan, tp_mesh):

    module_path_components = module_path.split(".*", 1)

    module = module.get_submodule(module_path_components[0])

    if len(module_path_components) == 1:
        
        if isinstance(plan, ParallelStyle):
            
            parallelize_on_state_dict_load(plan, module, tp_mesh)
        else:
            plan(module, tp_mesh)
    else:
        for _module in module:
            parallelize_module(_module, module_path_components[1], plan, tp_mesh)


def parallelize_model(
    model: torch.nn.Module, model_name: str, tp_mesh
) -> torch.nn.Module:

    model_plans = model_id_to_plans[model_name]

    for module_path, plan in model_plans.items():

        parallelize_module(model, module_path, plan, tp_mesh)

    return model
