import json
from functools import wraps
from typing import Any

import torch
from safetensors.torch import load_file
from torch.distributed._tensor import DTensor, Replicate, distribute_tensor
from tqdm import tqdm
from transformers.utils.hub import cached_file

from nnsight import util
from nnsight.intervention import InterventionProtocol


def load_hf_model_from_cache(model: torch.nn.Module, repo_id: str):

    model_index_filename = "model.safetensors.index.json"
    index_path = hf_hub_download(repo_id=repo_id, filename=model_index_filename)

    with open(index_path, "r") as f:
        index = json.load(f)

    shard_paths = sorted(set(index["weight_map"].values()))

    pbar = tqdm(shard_paths, desc="Loading shards")

    for shard_file in pbar:
        # Get path to shard
        shard_path = cached_file(
            repo_id=repo_id, filename=shard_file
        )
        pbar.set_postfix({"Current shard": shard_file})

        # Get path to shard
        state_dict = load_file(shard_path, device="cpu")

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

                    return tensor.redistribute(
                        placements=[Replicate()] * tensor.device_mesh.ndim,
                        async_op=False,
                    )

                placements.append(None)

                return tensor

            activations = util.apply(activations, check_for_dtensor, torch.Tensor)

            activations = intervene(activations, *args, **kwargs)

            def redistribute_tensors(tensor: torch.Tensor):

                nonlocal placements

                placement = placements.pop(0)

                if placement is None:

                    return tensor
                
                placement, device_mesh = placement

                if isinstance(tensor, DTensor):

                    return tensor.redistribute(
                        placements=placement, async_op=False
                    )

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
