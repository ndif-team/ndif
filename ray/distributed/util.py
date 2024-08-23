import json
from functools import wraps
from typing import Any, NamedTuple

import torch
from accelerate import load_checkpoint_and_dispatch
from accelerate.utils.imports import (
    is_mlu_available,
    is_mps_available,
    is_musa_available,
    is_npu_available,
    is_peft_available,
    is_torch_xla_available,
    is_xpu_available,
)
from accelerate.utils.modeling import check_device_same, clear_device_cache
from accelerate.utils import modeling
from safetensors.torch import load_file
from torch.distributed._tensor import DTensor, Replicate
from tqdm import tqdm
from transformers.utils.hub import cached_file

from nnsight import util
from nnsight.intervention import InterventionProtocol


def load_hf_model_from_cache(model: torch.nn.Module, repo_id: str):

    model_index_filename = "model.safetensors.index.json"
    index_path = cached_file(repo_id, model_index_filename)

    with open(index_path, "r") as f:
        index = json.load(f)

    shard_paths = sorted(set(index["weight_map"].values()))

    pbar = tqdm(shard_paths, desc="Loading shards")

    for shard_file in pbar:
        # Get path to shard
        shard_path = cached_file(repo_id, shard_file)
        pbar.set_postfix({"Current shard": shard_file})

        # Get path to shard
        state_dict = load_file(shard_path, device="cuda")

        model.load_state_dict(state_dict, strict=False, assign=True)

        torch.cuda.empty_cache()


def patch_intervention_protocol() -> None:

    def wrap(intervene):

        @wraps(intervene)
        def intervene_wrapper(activations: Any, *args, **kwargs):

            placements = []

            def check_for_dtensor(tensor: torch.Tensor):

                nonlocal placements

                if isinstance(tensor, DTensor):

                    placements.append((tensor.placements, tensor.device_mesh))

                    return tensor.to_local()

                placements.append(None)

                return tensor

            activations = util.apply(
                activations, check_for_dtensor, torch.Tensor
            )

            activations = intervene(activations, *args, **kwargs)

            def redistribute_tensors(tensor: torch.Tensor):

                nonlocal placements

                placement = placements.pop(0)

                if placement is None:

                    return tensor

                placement, device_mesh = placement

                return DTensor.from_local(
                    tensor, device_mesh=device_mesh, placements=placement
                )

            if len(placements) > 0:

                activations = util.apply(
                    activations, redistribute_tensors, torch.Tensor
                )

            return activations

        return intervene_wrapper

    InterventionProtocol.intervene = wrap(InterventionProtocol.intervene)


def to_full_tensor(data: Any) -> Any:

    return util.apply(data, lambda x: x.full_tensor(), DTensor)


def set_module_tensor_to_device(
    module: torch.nn.Module,
    tensor_name: str,
    device,
    value=None,
    dtype=None,
    fp16_statistics=None,
    tied_params_map=None,
):
    """
    A helper function to set a given tensor (parameter of buffer) of a module on a specific device (note that doing
    `param.to(device)` creates a new tensor not linked to the parameter, which is why we need this function).

    Args:
        module (`torch.nn.Module`):
            The module in which the tensor we want to move lives.
        tensor_name (`str`):
            The full name of the parameter/buffer.
        device (`int`, `str` or `torch.device`):
            The device on which to set the tensor.
        value (`torch.Tensor`, *optional*):
            The value of the tensor (useful when going from the meta device to any other device).
        dtype (`torch.dtype`, *optional*):
            If passed along the value of the parameter will be cast to this `dtype`. Otherwise, `value` will be cast to
            the dtype of the existing parameter in the model.
        fp16_statistics (`torch.HalfTensor`, *optional*):
            The list of fp16 statistics to set on the module, used for 8 bit model serialization.
        tied_params_map (Dict[int, Dict[torch.device, torch.Tensor]], *optional*, defaults to `None`):
            A map of current data pointers to dictionaries of devices to already dispatched tied weights. For a given
            execution device, this parameter is useful to reuse the first available pointer of a shared weight on the
            device for all others, instead of duplicating memory.
    """
    # Recurse if needed
    if "." in tensor_name:
        splits = tensor_name.split(".")
        for split in splits[:-1]:
            new_module = getattr(module, split)
            if new_module is None:
                raise ValueError(f"{module} has no attribute {split}.")
            module = new_module
        tensor_name = splits[-1]

    if (
        tensor_name not in module._parameters
        and tensor_name not in module._buffers
    ):
        raise ValueError(
            f"{module} does not have a parameter or a buffer named {tensor_name}."
        )
    is_buffer = tensor_name in module._buffers
    old_value = getattr(module, tensor_name)

    # Treat the case where old_value (or a custom `value`, typically offloaded to RAM/disk) belongs to a tied group, and one of the weight
    # in the tied group has already been dispatched to the device, by avoiding reallocating memory on the device and just copying the pointer.
    if (
        value is not None
        and tied_params_map is not None
        and value.data_ptr() in tied_params_map
        and device in tied_params_map[value.data_ptr()]
    ):
        module._parameters[tensor_name] = tied_params_map[value.data_ptr()][
            device
        ]
        return
    elif (
        tied_params_map is not None
        and old_value.data_ptr() in tied_params_map
        and device in tied_params_map[old_value.data_ptr()]
    ):
        module._parameters[tensor_name] = tied_params_map[old_value.data_ptr()][
            device
        ]
        return

    if (
        old_value.device == torch.device("meta")
        and device not in ["meta", torch.device("meta")]
        and value is None
    ):
        raise ValueError(
            f"{tensor_name} is on the meta device, we need a `value` to put in on {device}."
        )

    param = (
        module._parameters[tensor_name]
        if tensor_name in module._parameters
        else None
    )
    param_cls = type(param)

    if value is not None:
        # We can expect mismatches when using bnb 4bit since Params4bit will reshape and pack the weights.
        # In other cases, we want to make sure we're not loading checkpoints that do not match the config.
        if (
            old_value.shape != value.shape
            and param_cls.__name__ != "Params4bit"
        ):
            raise ValueError(
                f'Trying to set a tensor of shape {value.shape} in "{tensor_name}" (which has shape {old_value.shape}), this looks incorrect.'
            )

        if dtype is None:
            # For compatibility with PyTorch load_state_dict which converts state dict dtype to existing dtype in model
            value = value.to(old_value.dtype)
        elif not str(value.dtype).startswith(
            ("torch.uint", "torch.int", "torch.bool")
        ):
            value = value.to(dtype)

    device_quantization = None
    with torch.no_grad():
        # leave it on cpu first before moving them to cuda
        # # fix the case where the device is meta, we don't want to put it on cpu because there is no data =0
        if (
            param is not None
            and param.device.type != "cuda"
            and torch.device(device).type == "cuda"
            and param_cls.__name__ in ["Int8Params", "FP4Params", "Params4bit"]
        ):
            device_quantization = device
            device = "cpu"
        # `torch.Tensor.to(<int num>)` is not supported by `torch_npu` (see this [issue](https://github.com/Ascend/pytorch/issues/16)).
        if isinstance(device, int):
            if is_npu_available():
                device = f"npu:{device}"
            elif is_mlu_available():
                device = f"mlu:{device}"
            elif is_musa_available():
                device = f"musa:{device}"
            elif is_xpu_available():
                device = f"xpu:{device}"
        if "xpu" in str(device) and not is_xpu_available():
            raise ValueError(
                f'{device} is not available, you should use device="cpu" instead'
            )
        if value is None:
            new_value = old_value.to(device)
            if dtype is not None and device in ["meta", torch.device("meta")]:
                if not str(old_value.dtype).startswith(
                    ("torch.uint", "torch.int", "torch.bool")
                ):
                    new_value = new_value.to(dtype)

                if not is_buffer:
                    module._parameters[tensor_name] = param_cls(
                        new_value, requires_grad=old_value.requires_grad
                    )
        elif isinstance(value, torch.Tensor):
            new_value = value.to(device)
        else:
            new_value = torch.tensor(value, device=device)
        if device_quantization is not None:
            device = device_quantization
        if is_buffer:
            module._buffers[tensor_name] = new_value
        elif value is not None or not check_device_same(
            torch.device(device), module._parameters[tensor_name].device
        ):
            param_cls = type(module._parameters[tensor_name])
            kwargs = module._parameters[tensor_name].__dict__
            if param_cls.__name__ in ["Int8Params", "FP4Params", "Params4bit"]:
                if (
                    param_cls.__name__ == "Int8Params"
                    and new_value.dtype == torch.float32
                ):
                    # downcast to fp16 if any - needed for 8bit serialization
                    new_value = new_value.to(torch.float16)
                # quantize module that are going to stay on the cpu so that we offload quantized weights
                if device == "cpu" and param_cls.__name__ == "Int8Params":
                    new_value = (
                        param_cls(
                            new_value,
                            requires_grad=old_value.requires_grad,
                            **kwargs,
                        )
                        .to(0)
                        .to("cpu")
                    )
                    new_value.CB = new_value.CB.to("cpu")
                    new_value.SCB = new_value.SCB.to("cpu")
                else:
                    new_value = param_cls(
                        new_value,
                        requires_grad=old_value.requires_grad,
                        **kwargs,
                    ).to(device)
            elif param_cls.__name__ in ["QTensor", "QBitsTensor"]:
                new_value = torch.nn.Parameter(
                    new_value, requires_grad=old_value.requires_grad
                ).to(device)
            else:
                new_value = param_cls(
                    new_value, requires_grad=old_value.requires_grad
                ).to(device)

            module._parameters[tensor_name] = new_value
            if fp16_statistics is not None:
                module._parameters[tensor_name].SCB = fp16_statistics.to(device)
                del fp16_statistics
            # as we put the weight to meta, it doesn't have SCB attr anymore. make sure that it is not a meta weight
            if (
                module.__class__.__name__ == "Linear8bitLt"
                and getattr(module.weight, "SCB", None) is None
                and str(module.weight.device) != "meta"
            ):
                # quantize only if necessary
                device_index = (
                    torch.device(device).index
                    if torch.device(device).type == "cuda"
                    else None
                )
                if (
                    not getattr(module.weight, "SCB", None)
                    and device_index is not None
                ):
                    if (
                        module.bias is not None
                        and module.bias.device.type != "meta"
                    ):
                        # if a bias exists, we need to wait until the bias is set on the correct device
                        module = module.cuda(device_index)
                    elif module.bias is None:
                        # if no bias exists, we can quantize right away
                        module = module.cuda(device_index)
            elif (
                module.__class__.__name__ == "Linear4bit"
                and getattr(module.weight, "quant_state", None) is None
                and str(module.weight.device) != "meta"
            ):
                # quantize only if necessary
                device_index = (
                    torch.device(device).index
                    if torch.device(device).type == "cuda"
                    else None
                )
                if (
                    not getattr(module.weight, "quant_state", None)
                    and device_index is not None
                ):
                    module.weight = module.weight.cuda(device_index)
    # clean pre and post foward hook
    if device != "cpu":
        clear_device_cache()

    # When handling tied weights, we update tied_params_map to keep track of the tied weights that have already been allocated on the device in
    # order to avoid duplicating memory, see above.
    if (
        tied_params_map is not None
        and old_value.data_ptr() in tied_params_map
        and device not in tied_params_map[old_value.data_ptr()]
    ):
        tied_params_map[old_value.data_ptr()][device] = new_value
    elif (
        value is not None
        and tied_params_map is not None
        and value.data_ptr() in tied_params_map
        and device not in tied_params_map[value.data_ptr()]
    ):
        tied_params_map[value.data_ptr()][device] = new_value
        
        
    #### PATCH #######################################
    
    for hook in module._load_state_dict_post_hooks.values():
                
        hook(module, None)


def patch_accelerate():
    
    modeling.set_module_tensor_to_device = set_module_tensor_to_device