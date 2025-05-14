from dataclasses import dataclass, field
from datetime import timedelta
from timeit import default_timer as timer
from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Union

import torch
import torch.distributed
import torch.distributed.launch

import nnsight

from ..parallel_dims import ParallelDims
from ..util import load_hf_model_from_cache
from . import parallelize_model


def main(local_rank: int, world_rank: int, world_size: int, model_id: str):

    device = torch.device(f"cuda:{local_rank}")
    
    nnsight_model = nnsight.LanguageModel(model_id)

    torch.distributed.init_process_group(
        "nccl",
        init_method="tcp://10.201.22.179:5003",
        timeout=timedelta(seconds=10),
        world_size=world_size,
        rank=world_rank,

    )

    parallel_dims = ParallelDims(
        dp=1,
        tp=world_size,
        pp=1,
        world_size=world_size,
        enable_loss_parallel=False,
    )
    world_mesh = parallel_dims.build_mesh(device_type=f"cuda")

    model = nnsight_model._model
    
    torch.set_default_device(device)

    model = parallelize_model(model, model_id, world_mesh["tp"])
        
    load_hf_model_from_cache(model, model_id)
    
    nnsight_model._dispatched = True
    
    with nnsight_model.trace("hello", scan=False, validate=False):

        output = nnsight_model.model.layers[0].self_attn.q_proj.output.save()
        
    breakpoint()


if __name__ == "__main__":

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("local_rank", type=int)
    parser.add_argument("world_rank", type=int)
    parser.add_argument("world_size", type=int)
    parser.add_argument("--model_id", default="meta-llama/Meta-Llama-3-8B")
    
    main(**vars(parser.parse_args()))
